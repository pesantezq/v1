#!/usr/bin/env python3
"""
StockBot Operator Dashboard  (v2 -- enhanced)
=============================================
Streamlit-based management UI for the portfolio automation system.

Pages (sidebar):
  Dashboard | Run Controls | Outputs | Watchlist | Run History
  API Health | Config Editor | Prompts | Logs | Diagnostics

Launch (from project root):
    streamlit run gui/app.py
"""

import io
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import streamlit as st
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
from agent.llm_adapters import validate_openai_connection
from gui_operator_data import (
    load_operator_dashboard_data,
    load_profit_attribution,
    load_rotation_events,
    _compact_decision_reason,
    _ai_validation_badge,
    _get_insight_cards,
    load_data_quality_report,
    load_ai_budget_summary,
    load_confidence_calibration_latest,
    load_discovery_sandbox_status,
    load_automatic_promotion_data,
    load_news_evidence_layer,
    load_market_narrative_daily,
)
from portfolio_automation.discovery.approval_workflow import (
    ApprovalDecision,
    make_approval_decision,
    record_approval_decision,
)
from gui_insight_cards import render_insight_cards
from gui_insights import generate_insights as _generate_insights
from tools.weekly_report import generate_weekly_summary, markdown_to_plain_text
from watchlist_scanner.approved_config_loader import load_approved_weights

# -- Paths -------------------------------------------------------------------
ROOT             = Path(__file__).parent.parent.resolve()
CONFIG_PATH      = ROOT / "config.json"
ENV_PATH         = ROOT / ".env"
ENV_TEMPLATE     = ROOT / ".env.template"
OUTPUTS_LATEST   = ROOT / "outputs" / "latest"
OUTPUTS_HISTORY  = ROOT / "outputs" / "history"
LOGS_DIR         = ROOT / "logs"
DATA_DIR         = ROOT / "data"
TESTS_DIR        = ROOT / "tests"
PYTHON           = sys.executable   # same venv that runs the GUI

# New paths (v2)
PROMPTS_PATH     = DATA_DIR / "prompts.json"
WL_TAGS_PATH     = DATA_DIR / "watchlist_tags.json"
WL_CALL_COUNTER  = DATA_DIR / "watchlist_cache" / "call_counter.json"

# -- Page config -------------------------------------------------------------
st.set_page_config(
    page_title="StockBot Dashboard",
    page_icon="\U0001f4c8",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================================
# PURE HELPERS  -- no st.* calls, safe to call from anywhere
# ============================================================================

def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _load_config() -> dict:
    """Re-read config.json on every call (never cached -- edits are instant)."""
    return _load_json(CONFIG_PATH)


def _save_config(data: dict):
    """Write config.json. Returns None on success, error string on failure."""
    try:
        CONFIG_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return None
    except Exception as e:
        return str(e)


def _file_age(path: Path) -> str:
    if not path.exists():
        return "N/A"
    secs = (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds()
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _csv_to_df(path: Path) -> pd.DataFrame:
    """
    Parse a CSV that may have '#' comment lines or a trailing SUMMARY block.
    Drops fully-empty rows produced by blank lines in the file.
    """
    if not path.exists():
        return pd.DataFrame()
    try:
        lines, header_done = [], False
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not header_done:
                lines.append(line)
                header_done = True
                continue
            if line.startswith("#") or line.startswith("SUMMARY"):
                break
            lines.append(line)
        df = pd.read_csv(io.StringIO("\n".join(lines)))
        return df.dropna(how="all").reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def _fmt_usd(x) -> str:
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return str(x) if x is not None else ""


def _fmt_pct(x) -> str:
    try:
        return f"{float(x) * 100:.2f}%"
    except (TypeError, ValueError):
        return str(x) if x is not None else ""


def _run_command(cmd: list, timeout: int = 360):
    """Blocking subprocess call. Returns (returncode, stdout+stderr)."""
    try:
        res = subprocess.run(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        return res.returncode, res.stdout
    except subprocess.TimeoutExpired:
        return -1, f"[TIMEOUT] Process killed after {timeout}s"
    except Exception as e:
        return -1, f"[ERROR] {e}"


def _env_status() -> dict:
    """Return {key: {desc, set}} without exposing values."""
    keys = {
        "EMAIL_PASSWORD":        "Gmail SMTP app-password -- email digest",
        "FMP_API_KEY":           "S&P 500 scanner via Financial Modeling Prep",
        "ANTHROPIC_API_KEY":     "Claude AI agent -- monthly memo",
    }
    file_vals: dict = {}
    if ENV_PATH.exists():
        for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            file_vals[k.strip()] = v.strip()

    out = {}
    for key, desc in keys.items():
        val = os.environ.get(key, "") or file_vals.get(key, "")
        out[key] = {"desc": desc, "set": bool(val) and not val.startswith("your_")}
    return out


def _get_api_key(key_name: str) -> str:
    """Read a key from environment then .env file."""
    val = os.environ.get(key_name, "")
    if val:
        return val
    if ENV_PATH.exists():
        for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key_name:
                return v.strip()
    return ""


def _query_db(sql: str, db: Path = DATA_DIR / "portfolio.db") -> list:
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(sql).fetchall()]
        con.close()
        return rows
    except Exception:
        return []


def _render_file(fpath: Path) -> None:
    """Render a file in the appropriate Streamlit widget for its extension."""
    content = _read_text(fpath)
    ext = fpath.suffix.lower()

    if ext == ".csv":
        clean = [ln for ln in content.splitlines()
                 if not ln.startswith("#") and not ln.startswith("SUMMARY")]
        try:
            df = pd.read_csv(io.StringIO("\n".join(clean))).dropna(how="all")
            st.dataframe(df, width="stretch")
        except Exception:
            st.text_area("Raw", content, height=400)

    elif ext == ".md":
        st.markdown(content)

    elif ext == ".json":
        try:
            st.json(json.loads(content), expanded=2)
        except Exception:
            st.text_area("Raw", content, height=400)

    elif ext in (".txt", ".log"):
        st.code(content, language=None)

    else:
        st.text_area("Content", content, height=400)


def _store_run(rc: int, out: str, label: str) -> None:
    st.session_state.update(run_rc=rc, run_out=out, run_label=label)


def _fmt_ratio_pct(x) -> str:
    try:
        if x is None:
            return "Unknown"
        value = float(x)
        if abs(value) <= 1.5:
            return f"{value * 100:.1f}%"
        return f"{value:.1f}%"
    except (TypeError, ValueError):
        return "Unknown"


def _operator_dashboard_css() -> None:
    st.markdown(
        """
        <style>
        .operator-card {
            border: 1px solid rgba(49, 51, 63, 0.16);
            border-radius: 14px;
            padding: 1rem 1rem 0.85rem 1rem;
            background: linear-gradient(180deg, rgba(250,250,250,0.92), rgba(244,247,250,0.92));
            min-height: 132px;
            margin-bottom: 0.75rem;
        }
        .operator-label {
            font-size: 0.75rem;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            color: #5f6b7a;
            margin-bottom: 0.3rem;
        }
        .operator-value {
            font-size: 1.45rem;
            font-weight: 700;
            color: #16202b;
            line-height: 1.15;
        }
        .operator-subtle {
            margin-top: 0.45rem;
            color: #5f6b7a;
            font-size: 0.88rem;
        }
        .operator-badge {
            display: inline-block;
            border-radius: 999px;
            padding: 0.2rem 0.55rem;
            font-size: 0.76rem;
            font-weight: 600;
            margin: 0 0.35rem 0.35rem 0;
            border: 1px solid transparent;
        }
        .operator-badge.good {
            background: #e7f7ee;
            color: #146c43;
            border-color: #c2ebd1;
        }
        .operator-badge.warn {
            background: #fff4de;
            color: #8c5b00;
            border-color: #f4ddb0;
        }
        .operator-badge.bad {
            background: #fde8e8;
            color: #a61b1b;
            border-color: #f3c2c2;
        }
        .operator-badge.neutral {
            background: #edf2f7;
            color: #344054;
            border-color: #d5dde7;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _badge(text: str, tone: str = "neutral") -> str:
    safe_text = str(text).replace("<", "&lt;").replace(">", "&gt;")
    return f"<span class='operator-badge {tone}'>{safe_text}</span>"


def _render_operator_card(title: str, value: str, subtitle: str = "", badges: list[str] | None = None) -> None:
    badge_html = "".join(badges or [])
    st.markdown(
        (
            "<div class='operator-card'>"
            f"<div class='operator-label'>{title}</div>"
            f"<div class='operator-value'>{value}</div>"
            f"<div class='operator-subtle'>{subtitle}</div>"
            f"<div style='margin-top:0.6rem'>{badge_html}</div>"
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def _coerce_df(rows: list[dict], columns: list[str] | None = None) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=columns or [])
    df = pd.DataFrame(rows)
    if columns:
        ordered = [col for col in columns if col in df.columns]
        remainder = [col for col in df.columns if col not in ordered]
        return df[ordered + remainder]
    return df


def _coerce_num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _confidence_tone(value) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "neutral"
    if numeric >= 0.7:
        return "good"
    if numeric >= 0.4:
        return "warn"
    return "bad"


def _freshness_tone(status: str) -> str:
    mapping = {
        "fresh": "good",
        "stale": "warn",
        "old": "bad",
        "missing": "neutral",
    }
    return mapping.get(str(status or "").lower(), "neutral")


def _pretty_freshness(status: str) -> str:
    value = str(status or "missing").strip().lower()
    return value.title() if value else "Missing"


def _set_outputs_focus(target: dict | None) -> None:
    if not target:
        return
    scope = target.get("scope") or "Latest"
    file_name = target.get("file_name")
    st.session_state["nav_page"] = "Outputs"
    st.session_state["outputs_scope"] = scope
    if file_name:
        st.session_state["outputs_selected_file"] = file_name
    if target.get("label"):
        st.session_state["outputs_focus_label"] = target["label"]
    if target.get("relative_path"):
        st.session_state["outputs_focus_path"] = target["relative_path"]
    st.rerun()


def _render_outputs_action(target: dict | None, *, button_key: str, path_only: bool = False) -> None:
    if not target:
        return
    if not path_only and target.get("scope") in {"Latest", "Portfolio", "Policy", "Regime", "Reports"}:
        if st.button("Open in Outputs", key=button_key, width="stretch"):
            _set_outputs_focus(target)
    st.caption(f"Raw artifact: `{target.get('relative_path') or target.get('path') or 'Unavailable'}`")


def _render_freshness_strip(rows: list[dict], *, key_prefix: str) -> None:
    if not rows:
        st.info("No freshness metadata is available yet.")
        return

    cols = st.columns(len(rows))
    for idx, row in enumerate(rows):
        with cols[idx]:
            _render_operator_card(
                row.get("label", row.get("name", "Artifact")),
                row.get("updated_display", "Unknown"),
                f"{row.get('age_label', 'Unknown')} via {row.get('updated_source', 'missing')}",
                badges=[
                    _badge(_pretty_freshness(row.get("freshness_status")), _freshness_tone(row.get("freshness_status")))
                ],
            )
            _render_outputs_action(row.get("output_target"), button_key=f"{key_prefix}_open_{idx}")


def _render_memo_sections(memo: dict, *, key_prefix: str) -> None:
    sections = memo.get("sections") or []
    if not sections:
        st.info("No memo sections are available.")
        return

    labels = [
        f"{section['title']}{'' if section['found'] else ' (missing)'}"
        for section in sections
    ]
    selected_label = st.selectbox(
        "Jump to section",
        labels,
        key=f"{key_prefix}_memo_section_select",
    )
    selected_index = labels.index(selected_label)
    selected_section = sections[selected_index]
    if selected_section.get("found"):
        st.caption(f"Focused section: {selected_section['title']}")
    else:
        st.caption(f"{selected_section['title']} is not present in this memo.")

    index_badges = "".join(
        _badge(
            section["title"],
            "good" if section.get("found") else "neutral",
        )
        for section in sections
    )
    st.markdown(index_badges, unsafe_allow_html=True)

    for idx, section in enumerate(sections):
        with st.expander(
            f"{section['title']}{'' if section['found'] else ' (missing)'}",
            expanded=idx == selected_index,
        ):
            st.markdown(section.get("content") or "_No content available._")


def _render_interpretation(text: str) -> None:
    st.caption(text)


def _render_small_sample_notes(rows: list[dict]) -> None:
    warnings = []
    for row in rows:
        note = row.get("sample_warning")
        if note:
            label = row.get("bucket") or row.get("regime") or "bucket"
            warnings.append(f"{label}: {note}")
    if warnings:
        st.warning("Small-sample caution: " + "; ".join(warnings[:5]))


def _render_bar_chart_fallback(df: pd.DataFrame, *, index_col: str, value_cols: list[str], title: str) -> None:
    if df.empty:
        st.info("No data available.")
        return
    st.subheader(title)
    try:
        chart_df = df[[index_col] + [col for col in value_cols if col in df.columns]].copy()
        for col in value_cols:
            if col in chart_df.columns:
                chart_df[col] = chart_df[col].map(_coerce_num)
        st.bar_chart(chart_df.set_index(index_col))
    except Exception:
        st.dataframe(df, width="stretch", hide_index=True)


def _load_market_opportunities() -> dict:
    """Load market_opportunities.json from the latest outputs directory."""
    return _load_json(OUTPUTS_LATEST / "market_opportunities.json")


def _load_performance_summary() -> dict:
    """Load performance_summary.json from the performance outputs directory."""
    return _load_json(ROOT / "outputs" / "performance" / "performance_summary.json")


def _load_weight_tuning_suggestions() -> dict:
    """Load weight_tuning_suggestions.json from the performance outputs directory."""
    return _load_json(ROOT / "outputs" / "performance" / "weight_tuning_suggestions.json")


def _load_policy_simulation() -> dict:
    """Load policy_simulation.json from the performance outputs directory."""
    return _load_json(ROOT / "outputs" / "performance" / "policy_simulation.json")


def _load_config_proposal() -> dict:
    """Load config_proposal.json from the performance outputs directory."""
    return _load_json(ROOT / "outputs" / "performance" / "config_proposal.json")


def _load_approved_ranking_config() -> dict:
    """Load approved_ranking_config.json from the performance outputs directory."""
    return _load_json(ROOT / "outputs" / "performance" / "approved_ranking_config.json")


def _load_allocation_preview() -> dict:
    """Load allocation_policy_preview.json from the performance outputs directory."""
    return _load_json(ROOT / "outputs" / "performance" / "allocation_policy_preview.json")


def _load_allocation_policy_simulation() -> dict:
    """Load allocation_policy_simulation.json from the performance outputs directory."""
    return _load_json(ROOT / "outputs" / "performance" / "allocation_policy_simulation.json")


def _load_approved_allocation_policy() -> dict:
    """Load approved_allocation_policy.json from the performance outputs directory."""
    return _load_json(ROOT / "outputs" / "performance" / "approved_allocation_policy.json")


def _load_system_decision_summary() -> dict:
    """Load system_decision_summary.json from the latest outputs directory."""
    return _load_json(ROOT / "outputs" / "latest" / "system_decision_summary.json")


def _load_system_decision_summary_md() -> str:
    """Load system_decision_summary.md from the latest outputs directory."""
    return _read_text(ROOT / "outputs" / "latest" / "system_decision_summary.md")


def _action_tone(action: str) -> str:
    return {
        "BUY": "good",
        "PROMOTE_TO_PORTFOLIO": "good",
        "SELL": "bad",
        "TRIM": "warn",
        "ADD_TO_WATCHLIST": "neutral",
        "HOLD": "neutral",
    }.get(str(action).upper(), "neutral")


def _conviction_band_tone(band: str) -> str:
    return {
        "high_conviction": "good",
        "normal": "good",
        "starter": "warn",
        "observe": "warn",
        "defer": "bad",
        "suppressed": "bad",
    }.get(str(band).lower(), "neutral")


def _render_mc_freshness(mc: dict) -> None:
    """One-line 'data as of' caption for decision-data tabs."""
    path = OUTPUTS_LATEST / "market_opportunities.json"
    if not path.exists():
        st.caption(
            "Data: market_opportunities.json not found — run a market scan to populate this tab."
        )
        return
    st.caption(f"Data: market_opportunities.json updated {_file_age(path)}")


def _render_action_strip(mc: dict, bundle: dict) -> None:
    """Compact always-visible strip answering 'What should I do right now?'"""
    decision_layer    = mc.get("decision_layer") or {}
    actions           = decision_layer.get("actions") or []
    portfolio_rows    = bundle.get("portfolio_view", {}).get("rows") or []
    promoted          = mc.get("promoted") or []

    buy_actions  = [a for a in actions if a.get("action", "").upper() in {"BUY", "PROMOTE_TO_PORTFOLIO"}]
    sell_actions = [a for a in actions if a.get("action", "").upper() in {"SELL", "TRIM"}]

    portfolio_symbols = {r.get("ticker", "").upper() for r in portfolio_rows}
    rotation_candidates = sorted(
        [p for p in promoted if p.get("symbol", "").upper() not in portfolio_symbols],
        key=lambda x: x.get("score", 0),
        reverse=True,
    )

    mc_path   = OUTPUTS_LATEST / "market_opportunities.json"
    freshness = _file_age(mc_path) if mc_path.exists() else "no data yet"

    c1, c2, c3, c4, c5 = st.columns(5)

    with c1:
        top_buy = buy_actions[0] if buy_actions else None
        if top_buy:
            action_short = "PROMOTE" if "PROMOTE" in str(top_buy.get("action", "")).upper() else top_buy.get("action", "BUY")
            reason       = (top_buy.get("rationale") or [""])[0][:45]
            st.markdown(
                _badge(f"{action_short} {top_buy.get('symbol', '?')}", "good")
                + f"<br><small style='color:#5f6b7a'>score {top_buy.get('score', 0):.0f}"
                + (f" · {reason}" if reason else "") + "</small>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(_badge("No buy signals", "neutral"), unsafe_allow_html=True)
        st.caption("Top buy")

    with c2:
        top_sell = sell_actions[0] if sell_actions else None
        if top_sell:
            reason = (top_sell.get("rationale") or [""])[0][:50]
            st.markdown(
                _badge(f"{top_sell.get('action', 'SELL')} {top_sell.get('symbol', '?')}", "bad")
                + (f"<br><small style='color:#5f6b7a'>{reason}</small>" if reason else ""),
                unsafe_allow_html=True,
            )
        else:
            st.markdown(_badge("No exit signals", "good"), unsafe_allow_html=True)
        st.caption("Top exit")

    with c3:
        rot = rotation_candidates[0] if rotation_candidates else None
        if rot:
            st.markdown(
                _badge(f"ROTATE? {rot.get('symbol', '?')}", "warn")
                + f"<br><small style='color:#5f6b7a'>score {rot.get('score', 0):.0f} · not in portfolio</small>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(_badge("No rotation candidate", "neutral"), unsafe_allow_html=True)
        st.caption("Rotation")

    with c4:
        urgent    = len(sell_actions)
        total_act = len(buy_actions) + urgent
        tone      = "bad" if urgent > 1 else ("warn" if urgent == 1 else ("good" if total_act == 0 else "neutral"))
        st.markdown(
            _badge(f"{urgent} exit · {len(buy_actions)} buy", tone),
            unsafe_allow_html=True,
        )
        st.caption("Action count")

    with c5:
        tone = "neutral" if mc_path.exists() else "bad"
        st.markdown(_badge(freshness, tone), unsafe_allow_html=True)
        st.caption("Last scan")


def _render_portfolio_health_row(bundle: dict, mc: dict) -> None:
    """Single-row health status bar showing regime, action counts, and conviction summary."""
    portfolio_view = bundle.get("portfolio_view", {})
    overview       = bundle.get("overview", {})
    rows           = portfolio_view.get("rows") or []
    decision_layer = mc.get("decision_layer") or {}
    actions        = decision_layer.get("actions") or []

    regime      = overview.get("market_regime") or "—"
    regime_conf = overview.get("market_regime_confidence")
    regime_tone = _confidence_tone(regime_conf) if regime_conf is not None else "neutral"

    exit_flags     = len([a for a in actions if a.get("action", "").upper() in {"SELL", "TRIM"}])
    immediate_acts = len([a for a in actions if a.get("action", "").upper() in {"BUY", "PROMOTE_TO_PORTFOLIO", "SELL", "TRIM"}])
    high_conv      = len([r for r in rows if str(r.get("conviction_band", "")).lower() in {"high_conviction", "normal"}])
    top_sector     = str(portfolio_view.get("top_sector") or "—")[:15]
    regime_fit     = str(portfolio_view.get("portfolio_fit_vs_regime") or "—").replace("_", " ")
    fit_tone       = "warn" if "stretched" in regime_fit.lower() else ("bad" if "misaligned" in regime_fit.lower() else "good")

    h1, h2, h3, h4, h5 = st.columns(5)
    h1.markdown(_badge(f"Regime: {regime}", regime_tone), unsafe_allow_html=True)
    h2.markdown(
        _badge(
            f"{immediate_acts} action{'s' if immediate_acts != 1 else ''}",
            "bad" if immediate_acts > 3 else ("warn" if immediate_acts > 0 else "good"),
        ),
        unsafe_allow_html=True,
    )
    h3.markdown(
        _badge(
            f"{exit_flags} exit flag{'s' if exit_flags != 1 else ''}",
            "bad" if exit_flags > 0 else "good",
        ),
        unsafe_allow_html=True,
    )
    h4.markdown(
        _badge(
            f"{high_conv} high-conviction",
            "good" if high_conv >= 3 else ("warn" if high_conv > 0 else "neutral"),
        ),
        unsafe_allow_html=True,
    )
    h5.markdown(_badge(f"Fit: {regime_fit}", fit_tone), unsafe_allow_html=True)


def _load_signal_outcomes_df() -> pd.DataFrame:
    """Load signal_outcomes.csv — tracks every signal from emission to resolution."""
    return _csv_to_df(ROOT / "outputs" / "performance" / "signal_outcomes.csv")


def _load_profit_attribution() -> dict:
    """Load profit_attribution.json from the policy outputs directory."""
    return load_profit_attribution(ROOT)


def _load_rotation_events() -> list:
    """Load rotation_events.jsonl. Returns [] when absent or malformed."""
    return load_rotation_events(ROOT)


def _compute_system_confidence(perf_summary: dict) -> dict:
    """Heuristic system confidence from resolved performance data."""
    tracked  = int(_coerce_num(perf_summary.get("tracked_signals"), 0))
    resolved = int(_coerce_num(perf_summary.get("resolved_signals"), 0))

    if resolved < 5:
        return {
            "level": "BUILDING",
            "tone": "neutral",
            "reasons": [f"{tracked} signals tracked, {resolved} resolved — baseline building"],
        }

    by_window = perf_summary.get("by_window") or {}
    win_rates = [
        float(v.get("win_rate") or 0)
        for v in by_window.values()
        if isinstance(v, dict) and v.get("win_rate") is not None
    ]
    avg_wr = sum(win_rates) / len(win_rates) if win_rates else 0.0
    global_metrics = perf_summary.get("global_metrics") or {}
    hc_sr = global_metrics.get("high_confidence_success_rate")

    score = 0
    reasons: list[str] = []

    if avg_wr >= 0.6:
        score += 2
        reasons.append(f"win rate {avg_wr*100:.0f}% is strong")
    elif avg_wr >= 0.45:
        score += 1
        reasons.append(f"win rate {avg_wr*100:.0f}% is moderate")
    else:
        reasons.append(f"win rate {avg_wr*100:.0f}% needs improvement")

    if hc_sr is not None:
        try:
            hcsr = float(hc_sr)
            if hcsr >= 0.65:
                score += 2
                reasons.append("high-confidence signals are well-calibrated")
            elif hcsr >= 0.50:
                score += 1
                reasons.append("high-confidence signals show moderate calibration")
            else:
                reasons.append("high-confidence signals underperforming")
        except (TypeError, ValueError):
            pass

    if tracked >= 20:
        score += 1
        reasons.append(f"{tracked} tracked signals provide adequate sample coverage")

    if score >= 4:
        return {"level": "HIGH", "tone": "good", "reasons": reasons}
    if score >= 2:
        return {"level": "MEDIUM", "tone": "warn", "reasons": reasons}
    return {"level": "LOW", "tone": "bad", "reasons": reasons}


def _action_priority(action: dict) -> str:
    """Return HIGH / MEDIUM / LOW for a given action dict."""
    act        = str(action.get("action", "")).upper()
    score      = _coerce_num(action.get("score"), 0)
    confidence = _coerce_num(action.get("confidence"), 0)

    if act in {"SELL", "TRIM"}:
        return "HIGH"
    if act in {"BUY", "PROMOTE_TO_PORTFOLIO"}:
        if score >= 70 and confidence >= 0.75:
            return "HIGH"
        if score >= 55 or confidence >= 0.65:
            return "MEDIUM"
        return "LOW"
    return "LOW"


def _render_system_summary() -> None:
    """
    Compact System Summary strip at the top of the dashboard.

    Shows top theme, top opportunity, capital allocation delta, and policy status
    in a 4-column card row.  An expander reveals the full Markdown summary.
    Beginner-safe: skips silently when the summary file hasn't been generated yet.
    """
    summary = _load_system_decision_summary()
    md_text = _load_system_decision_summary_md()

    if not summary:
        st.info(
            "System summary not yet generated. "
            "Run `python -m watchlist_scanner.system_summary` to create it."
        )
        return

    gen_at = str(summary.get("generated_at") or "")
    gen_display = gen_at[:19].replace("T", " ") if gen_at else "unknown"

    tt  = summary.get("top_theme") or {}
    to  = summary.get("top_opportunity") or {}
    cp  = summary.get("capital_preview") or {}
    ss  = summary.get("system_state") or {}
    ch  = summary.get("changes") or {}

    summary_cols = st.columns(4)

    with summary_cols[0]:
        theme_name  = str(tt.get("name") or "—")
        theme_score = tt.get("score")
        theme_type  = str(tt.get("type") or "")
        _render_operator_card(
            "Top Theme",
            theme_name,
            f"{theme_type.title()} · Score {theme_score:.3f}" if theme_score is not None else theme_type.title(),
            badges=[
                _badge(
                    f"Persist {tt.get('persistence', 0.0):.2f}",
                    "good" if float(tt.get("persistence") or 0) >= 0.5 else "neutral",
                ) if tt else _badge("no data", "neutral"),
            ],
        )

    with summary_cols[1]:
        ticker = str(to.get("ticker") or "—")
        rank   = to.get("final_rank_score")
        fit    = str(to.get("portfolio_fit_label") or "—")
        mult   = to.get("rank_multiplier", 1.0)
        _render_operator_card(
            "Top Opportunity",
            ticker,
            f"Rank {rank:.3f} · Fit: {fit.title()}" if rank is not None else fit.title(),
            badges=[
                _badge(f"×{mult:.2f} multiplier", "good" if mult > 1.0 else "neutral"),
                _badge(str(to.get("conviction_band") or "—").replace("_", " "), "neutral"),
            ],
        )

    with summary_cols[2]:
        base_pct    = cp.get("total_baseline_pct")
        preview_pct = cp.get("total_preview_pct")
        delta       = cp.get("preview_vs_baseline_delta", 0.0)
        delta_tone  = "good" if delta > 0 else ("warn" if delta < 0 else "neutral")
        _render_operator_card(
            "Capital Preview",
            f"{preview_pct * 100:.1f}%" if preview_pct is not None else "—",
            f"Baseline {base_pct * 100:.1f}%" if base_pct is not None else "Baseline —",
            badges=[
                _badge(
                    f"Δ {delta * 100:+.2f}%",
                    delta_tone,
                ),
                _badge("advisory only", "warn"),
            ],
        )

    with summary_cols[3]:
        ws = str(ss.get("ranking_weights_source") or "default")
        ap = str(ss.get("allocation_policy_status") or "not_approved")
        change_count = int(ch.get("change_count") or 0)
        ws_tone = "good" if ws == "approved" else "neutral"
        ap_tone = "good" if ap == "approved_not_live" else "neutral"
        _render_operator_card(
            "Policy Status",
            ws.replace("_", " ").title(),
            ap.replace("_", " ").title(),
            badges=[
                _badge(f"weights: {ws}", ws_tone),
                _badge(f"allocation: {ap}", ap_tone),
                _badge(
                    f"{change_count} change{'s' if change_count != 1 else ''} detected",
                    "warn" if change_count > 0 else "neutral",
                ),
            ],
        )

    st.caption(f"System summary generated {gen_display}")

    if md_text:
        with st.expander("Full System Decision Summary (Markdown)", expanded=False):
            st.markdown(md_text)
    elif summary:
        with st.expander("Full System Decision Summary (JSON)", expanded=False):
            st.json(summary, expanded=1)


def _render_system_confidence_indicator(perf_summary: dict) -> None:
    """Single-line system confidence badge shown near the top of the dashboard."""
    conf = _compute_system_confidence(perf_summary)
    reason = conf["reasons"][0] if conf["reasons"] else ""
    st.markdown(
        _badge(f"System Confidence: {conf['level']}", conf["tone"])
        + (f"<small style='color:#5f6b7a;margin-left:0.6rem'>{reason}</small>" if reason else ""),
        unsafe_allow_html=True,
    )


def _render_daily_memo_section() -> None:
    """
    Daily Memo panel on the dashboard.

    'Generate Daily Memo' button builds plain-text and Markdown memos from the
    latest system_decision_summary.json and writes them to outputs/latest/.
    The preview uses st.code() which has a built-in copy button.
    """
    col_btn, col_status = st.columns([2, 8])
    with col_btn:
        generate_clicked = st.button("Generate Daily Memo", type="secondary", use_container_width=True)

    if generate_clicked:
        try:
            from watchlist_scanner.daily_memo import generate_daily_memo  # lazy import
            memo_txt, _ = generate_daily_memo()
            st.session_state["_daily_memo_text"] = memo_txt
            st.session_state["_daily_memo_error"] = None
        except Exception as exc:
            st.session_state["_daily_memo_error"] = str(exc)
            st.session_state["_daily_memo_text"] = None

    err  = st.session_state.get("_daily_memo_error")
    memo = st.session_state.get("_daily_memo_text")

    if err:
        with col_status:
            st.error(f"Memo generation failed: {err}")
    elif memo:
        with col_status:
            st.success("Memo written to outputs/latest/daily_memo.txt and daily_memo.md")
        with st.expander("Daily Memo Preview  (copy button top-right)", expanded=True):
            st.code(memo, language=None)


def _render_output_scope_browser(scope: str, base_dir: Path) -> None:
    if not base_dir.exists() or not any(base_dir.iterdir()):
        st.info(f"No files in `{base_dir.relative_to(ROOT)}/` yet.")
        return

    all_files = sorted([f for f in base_dir.iterdir() if f.is_file()])
    if not all_files:
        st.info(f"No files in `{base_dir.relative_to(ROOT)}/` yet.")
        return

    file_map = {f.name: f for f in all_files}
    desired = st.session_state.get("outputs_selected_file")
    options = list(file_map.keys())
    index = options.index(desired) if desired in options else 0
    select_key = f"outputs_select_{scope.lower()}"
    sel = st.selectbox("File to view", options, index=index, key=select_key)
    st.session_state["outputs_selected_file"] = sel

    focus_label = st.session_state.get("outputs_focus_label")
    focus_path = st.session_state.get("outputs_focus_path")
    if focus_label and focus_path and st.session_state.get("outputs_scope") == scope:
        st.info(f"Focused artifact: {focus_label}  |  `{focus_path}`")

    with st.expander(f"{len(all_files)} files in {base_dir.relative_to(ROOT)}/", expanded=False):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "File": f.name,
                        "Size": f"{f.stat().st_size:,} B",
                        "Age": _file_age(f),
                    }
                    for f in all_files
                ]
            ),
            hide_index=True,
            width="stretch",
        )

    fp = file_map[sel]
    st.caption(f"`{fp.relative_to(ROOT)}` -- {_file_age(fp)} -- {fp.stat().st_size:,} bytes")
    _render_file(fp)
    with open(fp, "rb") as fh:
        st.download_button(f"Download {sel}", fh.read(), file_name=sel)


def _render_overview_mode(bundle: dict, mc: dict) -> None:
    overview = bundle["overview"]
    memo = bundle["memo"]
    signal_triage = bundle["signal_triage"]
    run_status = bundle["run_status"]
    strategy = bundle["strategy_view"]

    top_row = st.columns(3)
    with top_row[0]:
        _render_operator_card(
            "Latest Run",
            overview["latest_run_status"].replace("_", " ").title(),
            f"{overview['last_updated']} ({overview['last_updated_age']})",
            badges=[
                _badge(f"Mode: {overview['run_mode']}", "neutral"),
                _badge(f"Data: {overview['data_mode']}", "warn" if overview["data_mode"] != "live" else "good"),
                _badge(
                    "Fallback Triggered",
                    "warn" if overview["status_badges"]["fallback_triggered"] else "neutral",
                ) if overview["status_badges"]["fallback_triggered"] else _badge("Fallback Clear", "good"),
            ],
        )
    with top_row[1]:
        _render_operator_card(
            "System State",
            "Degraded" if overview["degraded_mode"] else "Healthy",
            overview["degraded_reason"] or "No degraded reason active",
            badges=[
                _badge(
                    "Degraded Mode",
                    "warn" if overview["status_badges"]["degraded_mode"] else "good",
                ),
                _badge(
                    f"Regime: {overview['market_regime']}",
                    _confidence_tone(overview["market_regime_confidence"]),
                ),
                _badge(
                    f"Confidence: {_fmt_ratio_pct(overview['market_regime_confidence'])}",
                    _confidence_tone(overview["market_regime_confidence"]),
                ),
            ],
        )
    with top_row[2]:
        _render_operator_card(
            "Recommendation",
            f"{overview['profile']} / {overview['policy']}",
            f"Source: {overview['provider_source']}",
            badges=[
                _badge(
                    f"Confidence: {_fmt_ratio_pct(overview['recommendation_confidence'])}",
                    _confidence_tone(overview["recommendation_confidence"]),
                ),
                _badge(
                    "Low Confidence",
                    "bad",
                ) if overview["status_badges"]["low_recommendation_confidence"] else _badge("Confidence Acceptable", "good"),
            ],
        )

    # Quick action panel: answers "What should I buy / sell?" at a glance
    _dec = mc.get("decision_layer") or {}
    if _dec.get("available"):
        _all_acts = _dec.get("actions") or []
        _buy_acts  = [a for a in _all_acts if a.get("action", "").upper() in {"BUY", "PROMOTE_TO_PORTFOLIO"}]
        _sell_acts = [a for a in _all_acts if a.get("action", "").upper() in {"SELL", "TRIM"}]
        st.subheader("Quick Actions")
        qa_left, qa_right = st.columns(2)
        with qa_left:
            if _buy_acts:
                st.markdown("**Buy / Promote**")
                for _a in _buy_acts[:4]:
                    _s = _a.get("score")
                    _r = (_a.get("rationale") or [""])[0][:60]
                    st.markdown(
                        _badge(_a.get("action", "BUY"), "good")
                        + f" **{_a.get('symbol', '?')}**"
                        + (f" &nbsp;score {_s:.0f}" if _s else "")
                        + (f"<br><small style='color:#5f6b7a'>{_r}</small>" if _r else ""),
                        unsafe_allow_html=True,
                    )
            else:
                st.info("No active buy signals.")
        with qa_right:
            if _sell_acts:
                st.markdown("**Exit / Reduce**")
                for _a in _sell_acts[:4]:
                    _r = (_a.get("rationale") or [""])[0][:60]
                    st.markdown(
                        _badge(_a.get("action", "SELL"), _action_tone(_a.get("action", "SELL")))
                        + f" **{_a.get('symbol', '?')}**"
                        + (f"<br><small style='color:#5f6b7a'>{_r}</small>" if _r else ""),
                        unsafe_allow_html=True,
                    )
            else:
                st.success("No active exit signals.")
        st.caption(
            _dec.get("summary_line") or "Switch to Advanced → Decision Center for full detail."
        )

    st.subheader("Artifact Freshness")
    _render_freshness_strip(overview.get("freshness_strip", []), key_prefix="overview_freshness")

    st.subheader("Top Warnings")
    warnings = overview.get("top_warnings") or []
    if warnings:
        for warning in warnings:
            st.warning(warning)
    else:
        st.success("No major operator warnings surfaced from the latest artifacts.")

    left, right = st.columns([1.1, 1.4])
    with left:
        st.subheader("Memo Review")
        memo_view = st.radio(
            "Memo view",
            ["Simple", "Full"],
            horizontal=True,
            label_visibility="collapsed",
            key="overview_memo_view",
        )
        st.caption(
            f"{memo['title']}  |  {memo['age_label']}"
            if memo["available"]
            else "Latest memo artifact is optional and currently missing."
        )
        st.markdown(memo["simple_markdown"] if memo_view == "Simple" else (memo["full_markdown"] or "_No memo content._"))
        if memo_view == "Full":
            _render_memo_sections(memo, key_prefix="overview")

    with right:
        st.subheader("Signal Snapshot")
        if signal_triage["available"]:
            triage_df = _coerce_df(
                [
                    {
                        "Ticker": row["ticker"],
                        "Band": row["conviction_band"],
                        "Conviction": _fmt_ratio_pct(row["conviction_score"]),
                        "Effective": _fmt_ratio_pct(row["effective_score"]),
                        "Norm Alloc": _fmt_ratio_pct(row["normalized_allocation"]),
                        "Actionable": "Yes" if row["actionable_signal"] else "No",
                    }
                    for row in signal_triage["rows"][:8]
                ]
            )
            st.dataframe(triage_df, width="stretch", hide_index=True)
            counts_text = ", ".join(
                f"{band}: {count}" for band, count in signal_triage["counts_by_band"].items()
            )
            if counts_text:
                st.caption(f"Bands: {counts_text}")
            _render_outputs_action(signal_triage.get("output_target"), button_key="overview_signals_open")
        else:
            st.info("No watchlist signal artifact is available yet.")

    st.subheader("Freshness Detail")
    freshness_df = _coerce_df(
        [
            {
                "Artifact": row.get("label", row.get("name")),
                "Updated": row.get("updated_display", "Unknown"),
                "Status": _pretty_freshness(row.get("freshness_status")),
                "Age": row.get("age_label", "Unknown"),
                "Source": row.get("updated_source", "missing"),
            }
            for row in run_status.get("key_artifact_freshness", [])
        ]
    )
    if not freshness_df.empty:
        st.dataframe(freshness_df, width="stretch", hide_index=True)

    if strategy.get("output_target"):
        action_cols = st.columns(2)
        with action_cols[0]:
            _render_outputs_action(strategy["output_target"], button_key="overview_strategy_open")
        with action_cols[1]:
            portfolio_target = bundle["portfolio_view"].get("output_target")
            _render_outputs_action(portfolio_target, button_key="overview_portfolio_open")


def _render_run_status_tab(bundle: dict) -> None:
    run_status = bundle["run_status"]
    status_cols = st.columns(4)
    status_cols[0].metric("Last Successful Run", run_status["last_successful_run"])
    status_cols[1].metric("Latest Run Mode", run_status["latest_run_mode"])
    status_cols[2].metric("Provider Used", run_status["provider_used"])
    status_cols[3].metric("Actual Provider", run_status["actual_provider"])

    flag_cols = st.columns(4)
    flag_cols[0].markdown(
        _badge(
            f"Data fallback: {'on' if run_status['data_fallback_triggered'] else 'off'}",
            "warn" if run_status["data_fallback_triggered"] else "good",
        ),
        unsafe_allow_html=True,
    )
    flag_cols[1].markdown(
        _badge(
            f"LLM fallback: {'on' if run_status['llm_fallback_triggered'] else 'off'}",
            "warn" if run_status["llm_fallback_triggered"] else "good",
        ),
        unsafe_allow_html=True,
    )
    flag_cols[2].markdown(
        _badge(
            f"Fallback provider: {'yes' if run_status['fallback_occurred'] else 'no'}",
            "warn" if run_status["fallback_occurred"] else "good",
        ),
        unsafe_allow_html=True,
    )
    flag_cols[3].markdown(
        _badge(
            f"Watchlist source: {run_status['watchlist_source']}",
            "neutral",
        ),
        unsafe_allow_html=True,
    )

    st.subheader("Artifact Freshness")
    freshness_df = _coerce_df(
        [
            {
                "Artifact": row["artifact"],
                "Path": row["path"],
                "Available": "Yes" if row["available"] else "No",
                "Updated": row["updated_at"],
                "Freshness": _pretty_freshness(row["freshness_status"]),
                "Age": row["age"],
                "Source": row["updated_source"],
            }
            for row in run_status["artifact_freshness"]
        ]
    )
    st.dataframe(freshness_df, width="stretch", hide_index=True)

    if run_status["missing_artifact_warnings"]:
        st.warning("Missing artifacts: " + ", ".join(run_status["missing_artifact_warnings"]))
    else:
        st.success("All tracked operator artifacts are present.")

    with st.expander("Why?", expanded=False):
        st.json(
            {
                "provider_source": run_status["provider_source"],
                "data_mode": run_status["data_mode"],
                "data_sources_used": run_status["data_sources_used"],
                "degraded_mode": run_status["degraded_mode"],
                "degraded_reason": run_status["degraded_reason"],
                "model": run_status["model"],
            },
            expanded=False,
        )


def _render_memo_tab(bundle: dict) -> None:
    memo = bundle["memo"]
    st.subheader("Latest Memo")
    if not memo["available"]:
        st.info("No memo markdown was found. The dashboard continues without it.")
        return

    memo_view = st.radio(
        "Memo detail",
        ["Simple", "Full"],
        horizontal=True,
        label_visibility="collapsed",
        key="advanced_memo_view",
    )
    st.caption(f"{memo['path']}  |  {memo['age_label']}")
    st.markdown(memo["simple_markdown"] if memo_view == "Simple" else memo["full_markdown"])
    _render_memo_sections(memo, key_prefix="advanced")


def _render_signal_triage_tab(bundle: dict) -> None:
    triage = bundle["signal_triage"]
    if not triage["available"]:
        st.info("No watchlist signal rows are available.")
        return

    action_col, spacer_col = st.columns([1, 3])
    with action_col:
        _render_outputs_action(triage.get("output_target"), button_key="advanced_signals_open")

    band_order = ["high_conviction", "normal", "starter", "observe", "defer", "suppressed"]
    available_bands = [band for band in band_order if band in triage["counts_by_band"]]
    available_bands.extend(
        band for band in triage["counts_by_band"] if band not in available_bands
    )
    selected_bands = st.multiselect(
        "Conviction bands",
        available_bands,
        default=available_bands,
    )
    filtered_rows = [
        row for row in triage["rows"] if row["conviction_band"] in selected_bands
    ]
    if not filtered_rows:
        st.info("No signal rows match the current conviction-band filter.")
        return

    triage_df = _coerce_df(
        [
            {
                "Ticker": row["ticker"],
                "Band": row["conviction_band"],
                "Conviction": _fmt_ratio_pct(row["conviction_score"]),
                "Effective": _fmt_ratio_pct(row["effective_score"]),
                "Norm Alloc": _fmt_ratio_pct(row["normalized_allocation"]),
                "Theme": row.get("theme_alignment_label") or "—",
                "Top Theme": row.get("theme_top_name") or "—",
                "T.Matches": str(row.get("theme_match_count") or 0),
                "Aug.Score": _fmt_ratio_pct(row.get("augmented_signal_score")),
                "Fit": row.get("portfolio_fit_label") or "—",
                "Fit.Score": _fmt_ratio_pct(row.get("portfolio_fit_score")),
                "Sector": row["sector"],
                "Cooldown": "Yes" if row["cooldown_active"] else "No",
                "Reliability": row["signal_reliability"],
                "Actionable": "Yes" if row["actionable_signal"] else "No",
            }
            for row in filtered_rows
        ]
    )
    st.dataframe(triage_df, width="stretch", hide_index=True)

    if triage["summary_line"]:
        st.caption(triage["summary_line"])

    selected_ticker = st.selectbox(
        "Inspect signal",
        [row["ticker"] for row in filtered_rows],
        key="signal_triage_select",
    )
    selected_row = next((row for row in filtered_rows if row["ticker"] == selected_ticker), None)
    if selected_row:
        raw = selected_row["raw"]
        with st.expander("Why?", expanded=False):
            st.json(
                {
                    "ticker": selected_row["ticker"],
                    "conviction_band": selected_row["conviction_band"],
                    "conviction_inputs": raw.get("conviction_inputs"),
                    "conviction_caps_applied": raw.get("conviction_caps_applied"),
                    "score_breakdown": raw.get("score_breakdown"),
                    "alert_decision_reason": raw.get("alert_decision_reason"),
                    "cooldown_reason": raw.get("cooldown_reason"),
                    "priority_explanation": raw.get("priority_explanation"),
                    "theme_alignment_score": raw.get("theme_alignment_score"),
                    "augmented_signal_score": raw.get("augmented_signal_score"),
                    "theme_reason": raw.get("theme_reason"),
                    "theme_context": raw.get("theme_context"),
                    "portfolio_fit_score": raw.get("portfolio_fit_score"),
                    "portfolio_fit_reason": raw.get("portfolio_fit_reason"),
                    "portfolio_fit_context": raw.get("portfolio_fit_context"),
                    "final_rank_score": raw.get("final_rank_score"),
                },
                expanded=False,
            )


def _render_portfolio_tab(bundle: dict) -> None:
    portfolio_view = bundle["portfolio_view"]
    if not portfolio_view["available"]:
        st.info("Portfolio construction artifact is missing.")
        return

    action_col, spacer_col = st.columns([1, 3])
    with action_col:
        _render_outputs_action(portfolio_view.get("output_target"), button_key="advanced_portfolio_open")

    metrics = st.columns(4)
    metrics[0].metric("Suggested Allocation", _fmt_ratio_pct(portfolio_view["total_suggested_allocation"]))
    metrics[1].metric("Normalized Allocation", _fmt_ratio_pct(portfolio_view["total_normalized_allocation"]))
    metrics[2].metric("Capped Positions", str(portfolio_view["capped_positions"]))
    metrics[3].metric("Regime Fit", str(portfolio_view["portfolio_fit_vs_regime"]).replace("_", " ").title())

    sector_items = sorted(
        portfolio_view["allocation_by_sector"].items(),
        key=lambda item: item[1],
        reverse=True,
    )
    if sector_items:
        sector_df = _coerce_df(
            [
                {"Sector": sector, "Normalized Allocation": allocation}
                for sector, allocation in sector_items
            ]
        )
        sector_df["Normalized Allocation"] = sector_df["Normalized Allocation"].map(_coerce_num)
        st.subheader("Allocation by Sector")
        st.bar_chart(sector_df.set_index("Sector"))

    if portfolio_view["warnings"]:
        for warning in portfolio_view["warnings"]:
            st.warning(warning)
    else:
        st.success("No portfolio construction warnings were emitted.")

    if portfolio_view["regime_commentary"]:
        st.caption(portfolio_view["regime_commentary"])

    with st.expander("Why?", expanded=False):
        capped_rows = [
            row for row in portfolio_view["rows"]
            if row.get("allocation_capped")
        ]
        st.json(
            {
                "summary_line": portfolio_view["summary_line"],
                "top_sector": portfolio_view["top_sector"],
                "degraded_mode_impact": portfolio_view["degraded_mode_impact"],
                "capped_positions": capped_rows[:10],
                "groupings": portfolio_view["groupings"],
            },
            expanded=False,
        )


def _render_strategy_tab(bundle: dict) -> None:
    strategy = bundle["strategy_view"]
    if not strategy["available"]:
        st.info("No policy recommendation artifact was found. Recommendation panels stay read-only and optional.")
        return

    action_col, spacer_col = st.columns([1, 3])
    with action_col:
        _render_outputs_action(strategy.get("output_target"), button_key="advanced_strategy_open")

    metrics = st.columns(4)
    metrics[0].metric("Recommended Profile", strategy["recommended_profile"])
    metrics[1].metric("Recommended Policy", strategy["recommended_policy"])
    metrics[2].metric("Confidence", _fmt_ratio_pct(strategy["confidence"]))
    metrics[3].metric("Source", strategy["source"])

    st.subheader("Reasoning")
    for line in strategy["reasoning"]:
        st.markdown(f"- {line}")

    st.caption(f"Recommendation data quality: {strategy['data_quality']}")
    if strategy.get("quality_note"):
        st.warning(strategy["quality_note"])

    policy_alts = strategy["alternatives"].get("policies", []) if isinstance(strategy["alternatives"], dict) else []
    profile_alts = strategy["alternatives"].get("profiles", []) if isinstance(strategy["alternatives"], dict) else []
    if not isinstance(policy_alts, list):
        policy_alts = []
    if not isinstance(profile_alts, list):
        profile_alts = []
    alt_left, alt_right = st.columns(2)
    with alt_left:
        st.subheader("Alternative Policies")
        if policy_alts:
            alt_df = _coerce_df(
                [
                    {
                        "Policy": row.get("name"),
                        "Score": _fmt_ratio_pct(row.get("recommendation_score")),
                    }
                    for row in policy_alts
                ]
            )
            st.dataframe(alt_df, width="stretch", hide_index=True)
        else:
            st.caption("No alternative policy rankings available.")
    with alt_right:
        st.subheader("Alternative Profiles")
        if profile_alts:
            alt_df = _coerce_df(
                [
                    {
                        "Profile": row.get("name"),
                        "Score": _fmt_ratio_pct(row.get("recommendation_score")),
                    }
                    for row in profile_alts
                ]
            )
            st.dataframe(alt_df, width="stretch", hide_index=True)
        else:
            st.caption("No alternative profile rankings available.")

    with st.expander("Why?", expanded=False):
        st.json(
            {
                "inputs": strategy["why"]["inputs"],
                "source": strategy["why"]["source"],
                "quality_note": strategy["why"]["quality_note"],
                "evaluation_artifact": bool(strategy["evaluation"]),
                "outcomes_artifact": bool(strategy["outcomes"]),
            },
            expanded=False,
        )


def _render_health_tab(bundle: dict) -> None:
    health = bundle["health"]
    badges = [
        _badge(
            f"Degraded mode: {'on' if health['degraded_mode'] else 'off'}",
            "warn" if health["degraded_mode"] else "good",
        ),
        _badge(
            f"Data fallback: {'on' if health['fallback_usage']['data_fallback_triggered'] else 'off'}",
            "warn" if health["fallback_usage"]["data_fallback_triggered"] else "good",
        ),
        _badge(
            f"LLM fallback: {'on' if health['fallback_usage']['llm_fallback_triggered'] else 'off'}",
            "warn" if health["fallback_usage"]["llm_fallback_triggered"] else "good",
        ),
    ]
    st.markdown("".join(badges), unsafe_allow_html=True)

    if health["warnings"]:
        for warning in health["warnings"]:
            st.warning(warning)
    else:
        st.success("No health warnings surfaced from the tracked artifacts.")

    artifact_df = _coerce_df(
        [
            {
                "Artifact": name,
                "Path": status["path"],
                "Available": "Yes" if status["exists"] else "No",
                "Age": status["age_label"],
            }
            for name, status in health["artifact_availability"].items()
        ]
    )
    st.subheader("Artifact Availability")
    st.dataframe(artifact_df, width="stretch", hide_index=True)


def _render_performance_tab(bundle: dict) -> None:
    performance = bundle["performance_view"]
    st.subheader("Performance")
    _render_interpretation("Higher confidence should correspond to higher hit rates. Sample sizes are shown on every table and flagged when thin.")
    if not performance["available"]:
        st.info(
            "No resolved performance data yet. Signals need time to mature before stats appear — "
            "check back after a few trade cycles resolve."
        )
        _render_outputs_action(performance.get("output_target"), button_key="performance_open_missing")
        return

    action_col, spacer_col = st.columns([1, 3])
    with action_col:
        _render_outputs_action(performance.get("output_target"), button_key="performance_open")

    calibration_rows = performance.get("calibration_rows", [])
    calibration_df = _coerce_df(
        [
            {
                "Bucket": row["bucket"],
                "Hit Rate": _coerce_num(row["hit_rate"]),
                "Avg Return 5d": _coerce_num(row["avg_return_5d"]),
                "Sample": row["attributable_count"],
            }
            for row in calibration_rows
        ]
    )
    _render_small_sample_notes(calibration_rows)
    _render_bar_chart_fallback(
        calibration_df,
        index_col="Bucket",
        value_cols=["Hit Rate"],
        title="Confidence Calibration",
    )
    if not calibration_df.empty:
        st.dataframe(
            _coerce_df(
                [
                    {
                        "Bucket": row["bucket"],
                        "Hit Rate": _fmt_ratio_pct(row["hit_rate"]),
                        "Avg Return 5d": _fmt_ratio_pct(row["avg_return_5d"]),
                        "Median Return 5d": _fmt_ratio_pct(row["median_return_5d"]),
                        "Strong Win": _fmt_ratio_pct(row["strong_win_rate"]),
                        "Adverse": _fmt_ratio_pct(row["adverse_rate"]),
                        "Sample": row["attributable_count"],
                    }
                    for row in calibration_rows
                ]
            ),
            width="stretch",
            hide_index=True,
        )

    distribution = performance["return_distribution"]
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Avg Return 5d", _fmt_ratio_pct(distribution.get("avg_return_5d")))
    d2.metric("Median Return 5d", _fmt_ratio_pct(distribution.get("median_return_5d")))
    d3.metric("Strong Win Rate", _fmt_ratio_pct(distribution.get("strong_win_rate")))
    d4.metric("Adverse Rate", _fmt_ratio_pct(distribution.get("adverse_rate")))

    _render_interpretation("Coverage counts show how many recommendation records resolved at each horizon.")
    coverage_df = _coerce_df(
        [
            {"Horizon": row["horizon"], "Count": row["count"]}
            for row in performance.get("coverage_rows", [])
        ]
    )
    _render_bar_chart_fallback(coverage_df, index_col="Horizon", value_cols=["Count"], title="Coverage Metrics")
    if performance.get("notes"):
        for note in performance["notes"]:
            st.caption(note)


def _render_regime_analytics_tab(bundle: dict) -> None:
    regime_view = bundle["regime_analytics_view"]
    st.subheader("Regime")
    _render_interpretation("Use this view to check whether realized outcomes differ meaningfully across market regimes and whether degraded-data notes are accumulating.")
    if not regime_view["available"]:
        st.info(
            "No regime analytics data yet. This tab populates once signal outcomes have been "
            "resolved across different market regimes."
        )
        _render_outputs_action(regime_view.get("output_target"), button_key="regime_open_missing")
        return

    action_col, spacer_col = st.columns([1, 3])
    with action_col:
        _render_outputs_action(regime_view.get("output_target"), button_key="regime_open")

    regime_rows = regime_view.get("rows", [])
    regime_df = _coerce_df(
        [
            {
                "Regime": row["regime"],
                "Win Rate": _coerce_num(row["win_rate"]),
                "Avg Return": _coerce_num(row["avg_return_pct"]) / 100.0 if row.get("avg_return_pct") not in (None, "") else 0.0,
                "Sample": row["resolved_signals"] or row["total_signals"],
            }
            for row in regime_rows
        ]
    )
    _render_bar_chart_fallback(regime_df, index_col="Regime", value_cols=["Win Rate"], title="Win Rate by Regime")
    _render_bar_chart_fallback(regime_df, index_col="Regime", value_cols=["Avg Return"], title="Average Return by Regime")
    st.dataframe(
        _coerce_df(
            [
                {
                    "Regime": row["regime"],
                    "Total": row["total_signals"],
                    "Resolved": row["resolved_signals"],
                    "Win Rate": _fmt_ratio_pct(row["win_rate"]),
                    "Avg Return": f"{row['avg_return_pct']:+.2f}%" if isinstance(row.get("avg_return_pct"), (int, float)) else "Unknown",
                    "Best Band": row["best_conviction_band"],
                    "Worst Band": row["worst_conviction_band"],
                    "Degraded Note": row["degraded_note"] or "",
                }
                for row in regime_rows
            ]
        ),
        width="stretch",
        hide_index=True,
    )
    for note in regime_view.get("notes", []):
        st.caption(note)


def _render_signal_enrichment_tab(perf_summary: dict | None) -> None:
    st.subheader("Signal Enrichment Performance")
    _render_interpretation(
        "Evaluate whether theme alignment, portfolio fit, and final rank score actually improve "
        "signal outcomes. Buckets with fewer than 10 resolved samples are flagged — treat those as "
        "directional only."
    )
    if not perf_summary:
        st.info(
            "No performance data yet. Run a scan cycle and wait for signals to resolve before "
            "enrichment stats appear."
        )
        return

    primary_days = int(perf_summary.get("primary_window_days") or 3)
    window_label = f"{primary_days}d"

    def _bucket_table(buckets: dict, label_col: str) -> list[dict]:
        rows = []
        for name, stats in buckets.items():
            rows.append({
                label_col: name,
                "Count": stats.get("count") or 0,
                "Resolved": stats.get("resolved") or 0,
                f"Win Rate ({window_label})": _fmt_ratio_pct(stats.get("hit_rate")),
                f"Avg Return ({window_label})": f"{stats['avg_return']:+.2f}%" if stats.get("avg_return") is not None else "—",
                "Thin Sample": "⚠" if stats.get("low_sample_warning") else "",
            })
        return rows

    # ── Theme Alignment ──────────────────────────────────────────────────────
    st.markdown("#### Theme Alignment Performance")
    tap = perf_summary.get("theme_alignment_performance") or {}
    tap_buckets = tap.get("buckets") or {}
    if tap_buckets:
        st.dataframe(
            _coerce_df(_bucket_table(tap_buckets, "Alignment Tier")),
            width="stretch",
            hide_index=True,
        )
        st.caption(f"Total signals: {tap.get('total', 0)}")
    else:
        st.info("No theme alignment data in resolved signals.")

    # ── Portfolio Fit ────────────────────────────────────────────────────────
    st.markdown("#### Portfolio Fit Performance")
    pfp = perf_summary.get("portfolio_fit_performance") or {}
    pfp_buckets = pfp.get("buckets") or {}
    if pfp_buckets:
        st.dataframe(
            _coerce_df(_bucket_table(pfp_buckets, "Fit Label")),
            width="stretch",
            hide_index=True,
        )
        st.caption(f"Total signals: {pfp.get('total', 0)}")
    else:
        st.info("No portfolio fit data in resolved signals.")

    # ── Final Rank Score Quartiles ────────────────────────────────────────────
    st.markdown("#### Rank Score Performance (Quartiles)")
    frp = perf_summary.get("final_rank_performance") or {}
    frp_quartiles = frp.get("quartiles") or {}
    if frp_quartiles:
        q_rows = []
        for q_name, stats in frp_quartiles.items():
            q_rows.append({
                "Quartile": q_name,
                "Count": stats.get("count") or 0,
                "Resolved": stats.get("resolved") or 0,
                "Avg Rank Score": f"{stats['avg_final_rank_score']:.4f}" if stats.get("avg_final_rank_score") is not None else "—",
                f"Avg Return ({window_label})": f"{stats['avg_return']:+.2f}%" if stats.get("avg_return") is not None else "—",
                "Dir. Correct": _fmt_ratio_pct(stats.get("direction_correct_rate")),
                "Thin Sample": "⚠" if stats.get("low_sample_warning") else "",
            })
        st.dataframe(_coerce_df(q_rows), width="stretch", hide_index=True)
        st.caption(f"Q1 = top 25% by final_rank_score. Total scored: {frp.get('scored', 0)} / {frp.get('total', 0)}")
    else:
        st.info("No final_rank_score data in resolved signals.")

    # ── Theme Type ───────────────────────────────────────────────────────────
    st.markdown("#### Theme Type Performance")
    ttp = perf_summary.get("theme_type_performance") or {}
    ttp_types = ttp.get("by_type") or {}
    if ttp_types:
        st.dataframe(
            _coerce_df(_bucket_table(ttp_types, "Theme Type")),
            width="stretch",
            hide_index=True,
        )
        st.caption(f"Total signals: {ttp.get('total', 0)}")
    else:
        st.info("No theme type data in resolved signals.")

    # ── Weight Tuning Suggestions ─────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### Weight Tuning Suggestions")
    _render_interpretation(
        "Observe-only advisory. Candidate weight blends are back-tested against resolved signal "
        "history. No live scoring is changed. Run generate_weight_tuning_report() to refresh."
    )
    wt = _load_weight_tuning_suggestions()
    if not wt or not wt.get("candidates"):
        st.info(
            "No weight tuning data yet. Run "
            "`generate_weight_tuning_report()` from weight_tuning.py to produce suggestions."
        )
    else:
        recommended = wt.get("recommended_candidate", "—")
        reason = wt.get("recommendation_reason", "")
        resolved = int(wt.get("resolved_rows") or 0)
        col_a, col_b = st.columns([1, 3])
        with col_a:
            st.metric("Recommended Blend", recommended)
        with col_b:
            st.caption(reason)
            st.caption(
                f"Based on {resolved} resolved signal(s) | "
                "Observe-only — no config change applied."
            )
        wt_rows = []
        for c in wt.get("candidates", []):
            w = c.get("weights", {})
            weights_str = (
                f"aug:{w.get('augmented_signal_score', 0):.2f} "
                f"conf:{w.get('confidence_score', 0):.2f} "
                f"theme:{w.get('theme_alignment_score', 0):.2f} "
                f"fit:{w.get('portfolio_fit_score', 0):.2f}"
            )
            ret = c.get("top_quartile_avg_return")
            wt_rows.append({
                "Candidate": c.get("name", ""),
                "Weights": weights_str,
                f"Top-Q Hit Rate": _fmt_ratio_pct(c.get("top_quartile_hit_rate")),
                f"Top-Q Avg Return": f"{ret:+.2f}%" if ret is not None else "—",
                "Top-Q Dir. Correct": _fmt_ratio_pct(c.get("top_quartile_direction_correct_rate")),
                "Resolved (Top-Q)": c.get("sample_size", 0),
                "Thin Sample": "⚠" if c.get("low_sample_warning") else "",
            })
        st.dataframe(_coerce_df(wt_rows), width="stretch", hide_index=True)
        st.caption(
            "⚠ = fewer than 20 resolved signals in top quartile — treat as directional only."
        )

    # ── Policy Simulation ─────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### Policy Simulation")
    _render_interpretation(
        "Observe-only comparison of all ranking weight candidates against resolved history. "
        "Δ columns show improvement or degradation vs current weights. "
        "Run generate_policy_simulation_report() to refresh."
    )
    ps = _load_policy_simulation()
    if not ps or not ps.get("all_policies"):
        st.info(
            "No policy simulation data yet. Run "
            "`generate_policy_simulation_report()` from policy_simulation.py."
        )
    else:
        wl = f"{int(ps.get('primary_window_days') or 3)}d"
        recommended = ps.get("recommended_candidate", "—")
        resolved = int(ps.get("resolved_rows") or 0)
        col_a, col_b = st.columns([1, 3])
        with col_a:
            st.metric("Recommended Policy", recommended)
        with col_b:
            st.caption(f"Based on {resolved} resolved signal(s). Observe-only — no config changed.")

        ps_rows = []
        for p in ps.get("all_policies", []):
            d = p.get("delta_vs_current") or {}
            ret = p.get("top_quartile_avg_return")
            dhit = d.get("hit_rate")
            dret = d.get("avg_return")
            ddir = d.get("direction_correct_rate")
            ps_rows.append({
                "Rank": p.get("rank", "—"),
                "Candidate": p.get("name", ""),
                f"Hit Rate ({wl})": _fmt_ratio_pct(p.get("top_quartile_hit_rate")),
                f"Avg Return ({wl})": f"{ret:+.2f}%" if ret is not None else "—",
                "Dir. Correct": _fmt_ratio_pct(p.get("top_quartile_direction_correct_rate")),
                "Δ Hit Rate": f"{dhit:+.3f}" if dhit is not None else "—",
                "Δ Avg Return": f"{dret:+.3f}" if dret is not None else "—",
                "Δ Dir. Correct": f"{ddir:+.3f}" if ddir is not None else "—",
                "Resolved (Top-Q)": p.get("sample_size", 0),
                "Thin Sample": "⚠" if p.get("low_sample_warning") else "",
            })
        st.dataframe(_coerce_df(ps_rows), width="stretch", hide_index=True)
        st.caption("Rank 1 = best. Δ = delta vs current weights. ⚠ = fewer than 20 resolved in top quartile.")

        cur = ps.get("current_policy") or {}
        rec = ps.get("recommended_policy") or {}
        if cur and rec and cur.get("name") != rec.get("name"):
            st.markdown("**Current vs Recommended**")
            cmp_rows = []
            for policy in [cur, rec]:
                ret_ = policy.get("top_quartile_avg_return")
                cmp_rows.append({
                    "Policy": policy.get("name", ""),
                    f"Hit Rate ({wl})": _fmt_ratio_pct(policy.get("top_quartile_hit_rate")),
                    f"Avg Return ({wl})": f"{ret_:+.2f}%" if ret_ is not None else "—",
                    "Dir. Correct": _fmt_ratio_pct(policy.get("top_quartile_direction_correct_rate")),
                    "Resolved": policy.get("sample_size", 0),
                })
            st.dataframe(_coerce_df(cmp_rows), width="stretch", hide_index=True)

    # ── Config Proposal ───────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### Config Proposal")
    _render_interpretation(
        "Observe-only weight proposal derived from policy simulation. "
        "Not applied to live scoring. Inspect and apply manually if validated."
    )
    cp = _load_config_proposal()
    if not cp:
        st.info(
            "No config proposal yet. Run "
            "`generate_policy_simulation_report()` from policy_simulation.py."
        )
    else:
        st.markdown(_badge("NOT APPLIED — observe only", "warn"), unsafe_allow_html=True)
        st.caption(cp.get("advisory_note", ""))

        cand_name = cp.get("recommended_candidate", "—")
        reason = cp.get("recommendation_reason", "")
        col_a, col_b = st.columns([1, 3])
        with col_a:
            st.metric("Proposed Blend", cand_name)
        with col_b:
            if reason:
                st.caption(reason)

        proposed = cp.get("proposed_weights") or {}
        current_w = cp.get("current_weights") or {}
        deltas_w = cp.get("weight_deltas") or {}
        w_rows = []
        for k in current_w:
            label = k.replace("_score", "").replace("_", " ").title()
            w_rows.append({
                "Component": label,
                "Current": f"{current_w.get(k, 0):.2f}",
                "Proposed": f"{proposed.get(k, 0):.2f}",
                "Δ": f"{deltas_w.get(k, 0):+.2f}",
            })
        st.dataframe(_coerce_df(w_rows), width="stretch", hide_index=True)

        st.markdown("**Expected Performance Delta vs Current**")
        perf_d = cp.get("performance_delta") or {}
        pd_rows = [
            {
                "Metric": "Hit Rate",
                "Δ": f"{perf_d['hit_rate_delta']:+.3f}" if perf_d.get("hit_rate_delta") is not None else "—",
            },
            {
                "Metric": "Avg Return (%)",
                "Δ": f"{perf_d['avg_return_delta']:+.3f}" if perf_d.get("avg_return_delta") is not None else "—",
            },
            {
                "Metric": "Dir. Correct Rate",
                "Δ": f"{perf_d['direction_correct_rate_delta']:+.3f}" if perf_d.get("direction_correct_rate_delta") is not None else "—",
            },
        ]
        st.dataframe(_coerce_df(pd_rows), width="stretch", hide_index=True)

    # ── Approved Ranking Config ───────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### Approved Ranking Config")
    _render_interpretation(
        "A human-approved weight config that has passed review. "
        "Not applied to live scoring until manually wired in. "
        "Promote via: python -m watchlist_scanner.config_promotion --approve"
    )
    arc = _load_approved_ranking_config()
    _arc_weights = load_approved_weights(
        ROOT / "outputs" / "performance" / "approved_ranking_config.json"
    )
    _arc_weights_active = bool(_arc_weights and _arc_weights.get("_valid"))
    if not arc:
        st.info(
            "No approved config yet. Review config_proposal.json and run: "
            "`python -m watchlist_scanner.config_promotion --approve`"
        )
        st.markdown(
            _badge("Final rank weights: default (no approved config)", "neutral"),
            unsafe_allow_html=True,
        )
    else:
        st.markdown(_badge("NOT APPLIED TO LIVE SCORING", "warn"), unsafe_allow_html=True)
        if _arc_weights_active:
            _cand = (_arc_weights or {}).get("recommended_candidate") or "approved"
            st.markdown(
                _badge(
                    f"Final rank weights: approved ({_cand}) — affects ranking order only",
                    "good",
                ),
                unsafe_allow_html=True,
            )
        else:
            _reason = (_arc_weights or {}).get("reason", "config invalid")
            st.markdown(
                _badge(f"Final rank weights: default ({_reason})", "warn"),
                unsafe_allow_html=True,
            )

        meta_cols = st.columns(3)
        with meta_cols[0]:
            st.metric("Approved Candidate", arc.get("recommended_candidate", "—"))
        with meta_cols[1]:
            approved_at = arc.get("approved_at", "")
            st.metric("Approved At", approved_at[:19].replace("T", " ") if approved_at else "—")
        with meta_cols[2]:
            warn = arc.get("low_sample_warning")
            n = arc.get("sample_size")
            sample_str = f"{n}" if n is not None else "—"
            if warn:
                sample_str += " ⚠"
            st.metric("Sample Size (Top-Q)", sample_str)

        approved_weights = arc.get("proposed_weights") or {}
        current_weights = arc.get("current_weights") or {}
        weight_deltas = arc.get("weight_deltas") or {}
        aw_rows = []
        for k in current_weights:
            label = k.replace("_score", "").replace("_", " ").title()
            aw_rows.append({
                "Component": label,
                "Current": f"{current_weights.get(k, 0):.2f}",
                "Approved": f"{approved_weights.get(k, 0):.2f}",
                "Δ": f"{weight_deltas.get(k, 0):+.2f}",
            })
        st.dataframe(_coerce_df(aw_rows), width="stretch", hide_index=True)

        perf_d = arc.get("performance_delta") or {}
        if any(v is not None for v in perf_d.values()):
            st.markdown("**Expected Performance Delta vs Current**")
            ap_rows = [
                {"Metric": "Hit Rate", "Δ": f"{perf_d['hit_rate_delta']:+.3f}" if perf_d.get("hit_rate_delta") is not None else "—"},
                {"Metric": "Avg Return (%)", "Δ": f"{perf_d['avg_return_delta']:+.3f}" if perf_d.get("avg_return_delta") is not None else "—"},
                {"Metric": "Dir. Correct Rate", "Δ": f"{perf_d['direction_correct_rate_delta']:+.3f}" if perf_d.get("direction_correct_rate_delta") is not None else "—"},
            ]
            st.dataframe(_coerce_df(ap_rows), width="stretch", hide_index=True)

        st.caption(arc.get("approval_note", ""))
        if arc.get("low_sample_warning"):
            st.caption("⚠ Approved from thin sample — validate before applying to live scoring.")

    # ── Allocation Preview (Rank-Aware) ──────────────────────────────────────
    st.subheader("Allocation Preview (Rank-Aware)")
    _render_interpretation(
        "Simulates how final_rank_score would influence position sizing. "
        "Uses approved weights when active, otherwise default weights. "
        "Affects only ordering tiebreakers — not alert gating or live allocation."
    )
    st.markdown(
        _badge("Preview only — not applied to live allocation", "warn"),
        unsafe_allow_html=True,
    )
    ap = _load_allocation_preview()
    if not ap:
        st.info(
            "No allocation preview yet. Run: "
            "`python -m watchlist_scanner.allocation_preview` to generate."
        )
    else:
        ap_opps = ap.get("opportunities") or []
        ap_meta_cols = st.columns(3)
        with ap_meta_cols[0]:
            st.metric("Candidates", ap.get("candidate_count", len(ap_opps)))
        with ap_meta_cols[1]:
            total_base = ap.get("total_baseline_pct")
            st.metric("Total Baseline", f"{total_base:.1%}" if total_base is not None else "—")
        with ap_meta_cols[2]:
            total_prev = ap.get("total_preview_pct")
            st.metric("Total Preview", f"{total_prev:.1%}" if total_prev is not None else "—")

        if not ap_opps:
            st.info("No eligible signals in preview (filter_allowed + confidence threshold).")
        else:
            ap_rows = []
            for o in ap_opps:
                cap_str = ", ".join(o.get("capped_by") or []) or "—"
                ap_rows.append({
                    "Ticker": o.get("ticker", "—"),
                    "Rank Score": f"{o['final_rank_score']:.3f}" if o.get("final_rank_score") is not None else "—",
                    "Rank": o.get("rank_label", "—").title(),
                    "Multiplier": f"×{o['rank_multiplier']:.2f}" if o.get("rank_multiplier") is not None else "—",
                    "Baseline": f"{o['baseline_size']:.1%}" if o.get("baseline_size") is not None else "—",
                    "Preview Size": f"{o['preview_size']:.1%}" if o.get("preview_size") is not None else "—",
                    "Portfolio Fit": str(o.get("portfolio_fit_label") or "—").title(),
                    "Capped By": cap_str,
                    "Reason": o.get("reason", ""),
                })
            st.dataframe(_coerce_df(ap_rows), width="stretch", hide_index=True)

        gen_at = ap.get("generated_at", "")
        conf_thr = ap.get("confidence_threshold")
        caption_parts = []
        if gen_at:
            caption_parts.append(f"Generated {gen_at[:19].replace('T', ' ')}")
        if conf_thr is not None:
            caption_parts.append(f"confidence ≥ {conf_thr:.2f}")
        if caption_parts:
            st.caption(" · ".join(caption_parts))

    # ── Allocation Policy Simulation ─────────────────────────────────────────
    st.subheader("Allocation Policy Simulation")
    _render_interpretation(
        "Evaluates whether rank-aware allocation would have improved outcomes vs baseline "
        "allocation on resolved historical signals. Uses normalized_allocation as baseline "
        "and applies rank multipliers to compute the rank-aware counterfactual."
    )
    st.markdown(
        _badge("Simulation only — not applied to live allocation", "warn"),
        unsafe_allow_html=True,
    )
    sim = _load_allocation_policy_simulation()
    if not sim:
        st.info(
            "No simulation data yet. Run: "
            "`python -m watchlist_scanner.allocation_policy_simulation` to generate."
        )
    else:
        sim_sample = sim.get("sample_size", 0)
        sim_window = sim.get("primary_window_days", 3)
        st.caption(
            f"Based on {sim_sample} resolved signal{'s' if sim_sample != 1 else ''} "
            f"· Primary window: {sim_window}d"
        )
        b = sim.get("baseline") or {}
        ra = sim.get("rank_aware") or {}
        delta = sim.get("delta") or {}

        sim_top = st.columns(4)
        with sim_top[0]:
            st.metric("Sample Size", sim_sample)
        b_ret = b.get("total_return")
        ra_ret = ra.get("total_return")
        delta_ret = delta.get("total_return_delta")
        with sim_top[1]:
            st.metric("Total Return (Baseline)", f"{b_ret:.4f}" if b_ret is not None else "—")
        with sim_top[2]:
            st.metric(
                "Total Return (Rank-Aware)",
                f"{ra_ret:.4f}" if ra_ret is not None else "—",
                delta=f"{delta_ret:+.4f}" if delta_ret is not None else None,
            )
        with sim_top[3]:
            eff_delta = delta.get("efficiency_delta")
            st.metric("Efficiency Delta", f"{eff_delta:+.4f}" if eff_delta is not None else "—")

        sim_mid = st.columns(3)
        b_avg = b.get("avg_return_per_trade")
        ra_avg = ra.get("avg_return_per_trade")
        wc_delta = delta.get("win_capital_delta")
        with sim_mid[0]:
            st.metric("Avg Return/Trade (Baseline)", f"{b_avg:.4f}" if b_avg is not None else "—")
        with sim_mid[1]:
            st.metric("Avg Return/Trade (Rank-Aware)", f"{ra_avg:.4f}" if ra_avg is not None else "—")
        with sim_mid[2]:
            st.metric("Win Capital Delta", f"{wc_delta:+.4f}" if wc_delta is not None else "—")

        sim_eff = st.columns(2)
        b_eff = b.get("capital_efficiency")
        ra_eff = ra.get("capital_efficiency")
        with sim_eff[0]:
            st.metric(
                "Capital Efficiency (Baseline)",
                f"{b_eff:.4f}" if b_eff is not None else "—",
            )
        with sim_eff[1]:
            st.metric(
                "Capital Efficiency (Rank-Aware)",
                f"{ra_eff:.4f}" if ra_eff is not None else "—",
            )

        sim_details = sim.get("details") or []
        if sim_details:
            st.markdown("**Per-Signal Breakdown**")
            sim_rows = []
            for d in sim_details:
                sim_rows.append({
                    "Ticker": d.get("ticker", "—"),
                    "Return": f"{d['outcome_return']:.2f}%" if d.get("outcome_return") is not None else "—",
                    "Rank Score": f"{d['rank_score']:.3f}" if d.get("rank_score") is not None else "—",
                    "Rank": str(d.get("rank_label") or "—").title(),
                    "Multiplier": f"×{d['rank_multiplier']:.2f}" if d.get("rank_multiplier") is not None else "—",
                    "Baseline Size": f"{d['baseline_size']:.1%}" if d.get("baseline_size") is not None else "—",
                    "Preview Size": f"{d['preview_size']:.1%}" if d.get("preview_size") is not None else "—",
                    "Baseline Contrib": f"{d['baseline_contribution']:.4f}" if d.get("baseline_contribution") is not None else "—",
                    "Preview Contrib": f"{d['preview_contribution']:.4f}" if d.get("preview_contribution") is not None else "—",
                    "Win": "Y" if d.get("win") else "N",
                })
            st.dataframe(_coerce_df(sim_rows), width="stretch", hide_index=True)

        sim_gen_at = sim.get("generated_at", "")
        if sim_gen_at:
            st.caption(f"Generated {sim_gen_at[:19].replace('T', ' ')}")

    # ── Approved Allocation Policy ────────────────────────────────────────────
    st.subheader("Approved Allocation Policy")
    _render_interpretation(
        "Shows whether rank-aware allocation sizing has been formally approved based on "
        "simulation evidence. Approval is advisory only — applied_to_live is always false "
        "and live recommendations are unaffected unless explicitly configured."
    )
    aap = _load_approved_allocation_policy()
    if not aap:
        st.info(
            "No approved allocation policy yet. Run: "
            "`python -m watchlist_scanner.allocation_policy_activation --approve` "
            "to generate when simulation rules pass."
        )
        st.markdown(
            _badge("Rank-aware sizing: not activated", "neutral"),
            unsafe_allow_html=True,
        )
    else:
        activation_status = str(aap.get("activation_status") or "unknown")
        applied_to_live = aap.get("applied_to_live", False)
        tone = "good" if activation_status == "approved_not_live" else "warn"
        st.markdown(
            _badge(f"Activation status: {activation_status}", tone),
            unsafe_allow_html=True,
        )
        live_tone = "bad" if applied_to_live else "warn"
        st.markdown(
            _badge(
                f"applied_to_live: {applied_to_live} — advisory sizing only, not live",
                live_tone,
            ),
            unsafe_allow_html=True,
        )

        aap_approved_at = aap.get("approved_at", "")
        aap_sample = aap.get("sample_size")
        aap_window = aap.get("primary_window_days", 3)
        if aap_approved_at:
            st.caption(
                f"Approved {aap_approved_at[:19].replace('T', ' ')} "
                f"· {aap_sample} signals · {aap_window}d window"
            )

        aap_note = aap.get("approval_note", "")
        if aap_note:
            st.caption(f"Note: {aap_note}")

        aap_delta = aap.get("delta") or {}
        aap_b = aap.get("baseline") or {}
        aap_ra = aap.get("rank_aware") or {}
        aap_cols = st.columns(4)
        with aap_cols[0]:
            eff_delta = aap_delta.get("efficiency_delta")
            st.metric("Efficiency Delta", f"{eff_delta:+.4f}" if eff_delta is not None else "—")
        with aap_cols[1]:
            ret_delta = aap_delta.get("total_return_delta")
            st.metric("Return Delta", f"{ret_delta:+.4f}" if ret_delta is not None else "—")
        with aap_cols[2]:
            b_eff = aap_b.get("capital_efficiency")
            st.metric("Baseline Efficiency", f"{b_eff:.4f}" if b_eff is not None else "—")
        with aap_cols[3]:
            ra_eff = aap_ra.get("capital_efficiency")
            st.metric("Rank-Aware Efficiency", f"{ra_eff:.4f}" if ra_eff is not None else "—")

        rules_passed = list(aap.get("rules_passed") or [])
        rules_failed = list(aap.get("rules_failed") or [])
        if rules_passed or rules_failed:
            st.markdown("**Activation Rules**")
            rule_rows = []
            for r in rules_passed:
                rule_rows.append({"Rule": r, "Status": "PASS"})
            for r in rules_failed:
                rule_rows.append({"Rule": r, "Status": "FAIL"})
            st.dataframe(_coerce_df(rule_rows), width="stretch", hide_index=True)

    # ── Rank-Aware Advisory Sizing ────────────────────────────────────────────
    st.subheader("Rank-Aware Advisory Sizing")
    _render_interpretation(
        "Shows how advisory sizing would be enriched when an approved rank-aware allocation "
        "policy is active. The suggested_pct (actual sizing) is never changed — only advisory "
        "metadata fields (rank_aware_suggested_pct, allocation_policy_source) are added. "
        "No portfolio mutation, no alert-gating changes, no auto-trading."
    )
    st.markdown(
        _badge("Advisory only — suggested_pct is unchanged", "warn"),
        unsafe_allow_html=True,
    )
    _aap_live = _load_approved_allocation_policy()
    _aap_loader_valid = (
        isinstance(_aap_live, dict)
        and _aap_live.get("activation_status") == "approved_not_live"
        and _aap_live.get("applied_to_live") is not True
        and _aap_live.get("rank_aware") is not None
        and isinstance(_aap_live.get("delta"), dict)
        and float((_aap_live.get("delta") or {}).get("efficiency_delta") or 0.0) > 0.0
    )
    if not _aap_live:
        policy_src_label = "default"
        policy_src_tone = "neutral"
    elif _aap_loader_valid:
        policy_src_label = "approved_rank_aware"
        policy_src_tone = "good"
    else:
        policy_src_label = "default (policy invalid)"
        policy_src_tone = "warn"

    st.markdown(
        _badge(f"Policy source: {policy_src_label}", policy_src_tone),
        unsafe_allow_html=True,
    )

    if _aap_loader_valid and _aap_live:
        _ra_meta = _aap_live.get("rank_aware") or {}
        _b_meta = _aap_live.get("baseline") or {}
        _d_meta = _aap_live.get("delta") or {}

        advisory_cols = st.columns(3)
        with advisory_cols[0]:
            b_eff = _b_meta.get("capital_efficiency")
            st.metric(
                "Baseline Capital Efficiency",
                f"{b_eff:.4f}" if b_eff is not None else "—",
            )
        with advisory_cols[1]:
            ra_eff = _ra_meta.get("capital_efficiency")
            st.metric(
                "Rank-Aware Capital Efficiency",
                f"{ra_eff:.4f}" if ra_eff is not None else "—",
            )
        with advisory_cols[2]:
            eff_delta = _d_meta.get("efficiency_delta")
            st.metric(
                "Efficiency Delta",
                f"{eff_delta:+.4f}" if eff_delta is not None else "—",
            )

        sizing_cols = st.columns(2)
        with sizing_cols[0]:
            b_alloc = _b_meta.get("total_allocated_pct")
            st.metric(
                "Baseline Allocated %",
                f"{b_alloc:.1%}" if b_alloc is not None else "—",
            )
        with sizing_cols[1]:
            ra_alloc = _ra_meta.get("total_allocated_pct")
            st.metric(
                "Rank-Aware Allocated %",
                f"{ra_alloc:.1%}" if ra_alloc is not None else "—",
            )

        st.markdown("**Rank Multiplier Reference**")
        st.dataframe(
            _coerce_df([
                {"Rank Label": "Strong", "Score Threshold": "≥ 0.75", "Multiplier": "×1.25"},
                {"Rank Label": "Good", "Score Threshold": "≥ 0.55", "Multiplier": "×1.10"},
                {"Rank Label": "Neutral", "Score Threshold": "≥ 0.35", "Multiplier": "×1.00"},
                {"Rank Label": "Poor", "Score Threshold": "< 0.35", "Multiplier": "×0.75"},
            ]),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "When an approved policy is active, suggest_allocation() populates "
            "allocation_policy_source='approved_rank_aware', rank_multiplier, "
            "baseline_suggested_pct, and rank_aware_suggested_pct as advisory metadata. "
            "The suggested_pct field is never modified."
        )
    else:
        st.info(
            "No valid approved allocation policy found. Advisory sizing will use "
            "allocation_policy_source='default' with rank_aware_suggested_pct equal to "
            "baseline_suggested_pct and rank_multiplier=1.0."
        )


def _render_recommendation_quality_tab(bundle: dict) -> None:
    quality = bundle["recommendation_quality_view"]
    st.subheader("Recommendation Quality")
    _render_interpretation("Higher scores and higher confidence should trend toward better outcomes over time, but small samples can easily distort the picture.")
    if not quality["available"]:
        st.info(
            "No recommendation quality data yet. This tab populates once resolved trade "
            "outcomes are available for analysis."
        )
        _render_outputs_action(quality.get("output_targets", {}).get("outcomes"), button_key="quality_open_missing")
        return

    action_cols = st.columns(2)
    with action_cols[0]:
        _render_outputs_action(quality.get("output_targets", {}).get("outcomes"), button_key="quality_open_outcomes")
    with action_cols[1]:
        _render_outputs_action(quality.get("output_targets", {}).get("evaluation"), button_key="quality_open_eval")

    monotonicity = quality.get("monotonicity_label", "unavailable")
    tone = {"monotonic": "good", "mixed": "warn", "inverted": "bad"}.get(monotonicity, "neutral")
    st.markdown(_badge(f"Confidence vs outcome: {monotonicity}", tone), unsafe_allow_html=True)

    degraded_rows = quality.get("by_degraded_mode", [])
    _render_small_sample_notes(degraded_rows)
    degraded_df = _coerce_df(
        [
            {"Bucket": row["bucket"], "Hit Rate": _coerce_num(row["hit_rate"]), "Sample": row["attributable_count"]}
            for row in degraded_rows
        ]
    )
    _render_bar_chart_fallback(degraded_df, index_col="Bucket", value_cols=["Hit Rate"], title="Hit Rate by Degraded vs Normal")

    action_rows = quality.get("by_action_level", [])
    action_df = _coerce_df(
        [
            {"Action Level": row["bucket"], "Hit Rate": _coerce_num(row["hit_rate"]), "Sample": row["attributable_count"]}
            for row in action_rows
        ]
    )
    _render_bar_chart_fallback(action_df, index_col="Action Level", value_cols=["Hit Rate"], title="Hit Rate by Action Level")

    impact_rows = quality.get("by_impact_area", [])
    impact_df = _coerce_df(
        [
            {"Impact Area": row["bucket"], "Hit Rate": _coerce_num(row["hit_rate"]), "Sample": row["attributable_count"]}
            for row in impact_rows
        ]
    )
    _render_bar_chart_fallback(impact_df, index_col="Impact Area", value_cols=["Hit Rate"], title="Hit Rate by Impact Area")

    decile_rows = quality.get("by_score_decile", [])
    _render_small_sample_notes(decile_rows)
    decile_df = _coerce_df(
        [
            {
                "Score Bucket": row["bucket"],
                "Hit Rate": _coerce_num(row["hit_rate"]),
                "Avg Return 5d": _coerce_num(row["avg_return_5d"]),
                "Sample": row["attributable_count"],
            }
            for row in decile_rows
        ]
    )
    _render_bar_chart_fallback(decile_df, index_col="Score Bucket", value_cols=["Hit Rate"], title="Performance by Score Bucket")
    if not decile_df.empty:
        st.dataframe(
            _coerce_df(
                [
                    {
                        "Score Bucket": row["bucket"],
                        "Hit Rate": _fmt_ratio_pct(row["hit_rate"]),
                        "Avg Return 5d": _fmt_ratio_pct(row["avg_return_5d"]),
                        "Median Return 5d": _fmt_ratio_pct(row["median_return_5d"]),
                        "Sample": row["attributable_count"],
                    }
                    for row in decile_rows
                ]
            ),
            width="stretch",
            hide_index=True,
        )

    for note in quality.get("notes", []):
        st.caption(note)


def _render_data_quality_tab(bundle: dict) -> None:
    dq = bundle.get("data_quality_report", {})
    st.subheader("Data Quality Monitor")
    _render_interpretation(
        "Observe-only view of data quality for the last pipeline run. "
        "No scores, allocations, or recommendations are changed here."
    )
    st.caption("Source: `outputs/latest/data_quality_report.json` — observe-only, never modifies pipeline behavior.")

    if not dq.get("available"):
        st.info(dq.get("summary_line") or "Data quality report not available yet. Run the daily pipeline to generate it.")
        st.caption("Expected at: `outputs/latest/data_quality_report.json`")
        return

    total = dq.get("total_symbols", 0)
    healthy = dq.get("healthy_symbols", 0)
    warning = dq.get("warning_symbols", 0)
    critical = dq.get("critical_symbols", 0)

    if critical > 0:
        overall_tone = "bad"
        overall_label = "Critical"
    elif warning > 0:
        overall_tone = "warn"
        overall_label = "Warning"
    elif total > 0:
        overall_tone = "good"
        overall_label = "Healthy"
    else:
        overall_tone = "neutral"
        overall_label = "Unavailable"

    badges = [
        _badge(overall_label, overall_tone),
        _badge(f"{healthy}/{total} healthy", "good" if healthy == total else "neutral"),
    ]
    if warning > 0:
        badges.append(_badge(f"{warning} warning", "warn"))
    if critical > 0:
        badges.append(_badge(f"{critical} critical", "bad"))
    st.markdown("".join(badges), unsafe_allow_html=True)
    st.caption(dq.get("summary_line", ""))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Symbols", total)
    c2.metric("Healthy", healthy)
    c3.metric("Warning", warning)
    c4.metric("Critical", critical)

    issues = dq.get("issues") or []
    critical_issues = [i for i in issues if isinstance(i, dict) and i.get("severity") == "critical"]
    warning_issues = [i for i in issues if isinstance(i, dict) and i.get("severity") == "warning"]

    if critical_issues:
        with st.expander(f"Critical Issues ({len(critical_issues)})", expanded=True):
            for issue in critical_issues:
                st.error(f"**{issue.get('issue_type', 'UNKNOWN')}** — {issue.get('message', '')}")
    if warning_issues:
        with st.expander(f"Warnings ({len(warning_issues)})", expanded=False):
            for issue in warning_issues:
                st.warning(f"**{issue.get('issue_type', 'UNKNOWN')}** — {issue.get('message', '')}")
    if not critical_issues and not warning_issues:
        st.success("No critical or warning issues detected.")

    fallback = dq.get("fallback_count", 0)
    stale = dq.get("stale_price_count", 0)
    missing_price = dq.get("missing_price_count", 0)
    if any(v > 0 for v in (fallback, stale, missing_price)):
        st.caption(f"Fallback used: {fallback} | Stale prices: {stale} | Missing price: {missing_price}")


def _render_ai_budget_tab(bundle: dict) -> None:
    budget = bundle.get("ai_budget_summary", {})
    st.subheader("AI Budget Summary")
    _render_interpretation(
        "Observe-only tracking of AI/LLM call costs from the last pipeline run. "
        "Advisory and observability only — does not block or modify AI calls unless hard enforcement is enabled."
    )
    st.caption("Source: `outputs/latest/ai_budget_summary.json` — advisory/observability only.")

    if not budget.get("available"):
        st.info(budget.get("summary_line") or "AI budget summary not available yet. Run the daily pipeline to generate it.")
        st.caption("Expected at: `outputs/latest/ai_budget_summary.json`")
        return

    blocked = budget.get("blocked", False)
    warning = budget.get("warning", False)
    observe_only = budget.get("observe_only", True)

    if blocked:
        overall_tone, overall_label = "bad", "Blocked"
    elif warning:
        overall_tone, overall_label = "warn", "Warning"
    else:
        overall_tone, overall_label = "good", "Within Budget"

    badges = [
        _badge(overall_label, overall_tone),
        _badge("observe-only" if observe_only else "hard enforcement", "neutral" if observe_only else "warn"),
    ]
    st.markdown("".join(badges), unsafe_allow_html=True)
    st.caption(budget.get("summary_line", ""))

    daily_cost = budget.get("daily_cost_total_usd", 0.0)
    monthly_cost = budget.get("monthly_cost_total_usd", 0.0)
    daily_limit = budget.get("daily_cost_limit_usd")
    monthly_limit = budget.get("monthly_cost_limit_usd")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Daily Cost", f"${daily_cost:.4f}")
    c2.metric("Monthly Cost", f"${monthly_cost:.4f}")
    c3.metric("Daily Limit", f"${daily_limit:.4f}" if daily_limit is not None else "None")
    c4.metric("Monthly Limit", f"${monthly_limit:.4f}" if monthly_limit is not None else "None")

    warnings = budget.get("warnings") or []
    if warnings:
        for w in warnings:
            st.warning(w)

    event_count = budget.get("event_count", 0)
    daily_tokens = budget.get("daily_token_total", 0)
    st.caption(f"AI calls tracked today: {event_count} | Tokens: {daily_tokens:,}")


def _render_calibration_tab(bundle: dict) -> None:
    cal = bundle.get("confidence_calibration_latest", {})
    st.subheader("Confidence Calibration")
    _render_interpretation(
        "Observe-only calibration of historical confidence scores against resolved outcomes. "
        "This does not automatically change scoring — it surfaces calibration gaps for review only."
    )
    st.caption(
        "Source: `outputs/latest/confidence_calibration.json` — observe-only. "
        "Does not automatically change scoring or registry values."
    )

    if not cal.get("available"):
        st.info(cal.get("summary_line") or "Confidence calibration not available yet.")
        st.caption("Expected at: `outputs/latest/confidence_calibration.json`")
        return

    if cal.get("insufficient_data"):
        st.warning(
            "Insufficient resolved decisions to compute calibration. "
            f"Total resolved: {cal.get('total_resolved', 0)}. Check back after more signals resolve."
        )
        dq_warnings = cal.get("dq_warnings") or []
        for w in dq_warnings:
            st.caption(f"DQ note: {w}")
        return

    total = cal.get("total_resolved", 0)
    hit_rate = cal.get("overall_hit_rate")
    avg_return = cal.get("overall_avg_return")

    c1, c2, c3 = st.columns(3)
    c1.metric("Resolved Decisions", total)
    if hit_rate is not None:
        c2.metric("Overall Hit Rate", f"{hit_rate * 100:.1f}%")
    if avg_return is not None:
        c3.metric("Avg Return", f"{avg_return * 100:.2f}%")

    st.caption(cal.get("summary_line", ""))

    buckets_5 = cal.get("buckets_5") or []
    if buckets_5:
        st.subheader("5-Bucket Calibration")
        bucket_rows = []
        for b in buckets_5:
            if not isinstance(b, dict):
                continue
            hr = b.get("hit_rate")
            avg_r = b.get("avg_return_5d")
            bucket_rows.append({
                "Bucket": b.get("label", "unknown"),
                "Count": b.get("count", 0),
                "Resolved": b.get("attributable_count", 0),
                "Hit Rate": f"{hr * 100:.1f}%" if hr is not None else "—",
                "Avg Return 5d": f"{avg_r * 100:.2f}%" if avg_r is not None else "—",
                "Small Sample": "Yes" if b.get("small_sample") else "No",
            })
        if bucket_rows:
            st.dataframe(_coerce_df(bucket_rows), width="stretch", hide_index=True)

    signal_results = cal.get("signal_results") or []
    if signal_results:
        with st.expander(f"Per-Signal Calibration ({len(signal_results)} signals)", expanded=False):
            sig_rows = []
            for s in signal_results:
                if not isinstance(s, dict):
                    continue
                gap = s.get("calibration_gap")
                review = s.get("suggested_review", False)
                sig_rows.append({
                    "Signal": s.get("signal_source", "unknown"),
                    "Resolved": s.get("resolved_count", 0),
                    "Hit Rate": f"{s.get('hit_rate', 0) * 100:.1f}%" if s.get("hit_rate") is not None else "—",
                    "Avg Conf": f"{s.get('average_confidence', 0) * 100:.1f}%" if s.get("average_confidence") is not None else "—",
                    "Gap": f"{gap * 100:.1f}%" if gap is not None else "—",
                    "Review?": "Yes" if review else "No",
                })
            if sig_rows:
                st.dataframe(_coerce_df(sig_rows), width="stretch", hide_index=True)

    dq_warnings = cal.get("dq_warnings") or []
    if dq_warnings:
        with st.expander("Data Quality Notes", expanded=False):
            for w in dq_warnings:
                st.caption(w)


def _render_discovery_sandbox_tab(bundle: dict) -> None:
    disc = bundle.get("discovery_sandbox_status", {})
    st.subheader("Discovery Sandbox")
    st.markdown(
        "> **Research-only.** Discovery candidates are not buy/sell recommendations and are not part of the official watchlist or portfolio. "
        "No official portfolio state has been modified."
    )
    st.caption("Source: `outputs/sandbox/discovery/` — sandbox lane only. No official actions taken.")

    if not disc.get("available"):
        st.info(
            "No discovery sandbox artifacts found. "
            "Run the pipeline in `discovery` mode to generate research candidates."
        )
        st.caption("Expected at: `outputs/sandbox/discovery/`")
        return

    watch_count = disc.get("watch_count", 0)
    discovered_count = disc.get("discovered_count", 0)
    rejected_count = disc.get("total_rejected", 0)
    memory_count = disc.get("memory_entry_count", 0)

    badges = [
        _badge("research-only", "neutral"),
        _badge("sandbox", "neutral"),
        _badge("no trades", "neutral"),
    ]
    st.markdown("".join(badges), unsafe_allow_html=True)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Watch", watch_count)
    c2.metric("Discovered", discovered_count)
    c3.metric("Rejected", rejected_count)
    c4.metric("Memory Entries", memory_count)

    if disc.get("run_id"):
        st.caption(f"Run ID: `{disc['run_id']}`")

    watch_cands = disc.get("watch_candidates") or []
    if watch_cands:
        st.subheader("Watch Candidates")
        watch_rows = []
        for c in watch_cands:
            if not isinstance(c, dict):
                continue
            watch_rows.append({
                "Ticker": c.get("ticker", "?"),
                "Score": f"{c.get('score', 0):.2f}",
                "Event Type": c.get("event_type", "unknown"),
                "Mentions": c.get("mention_count", 0),
                "Sources": c.get("unique_source_count", 0),
                "Risk Flag": "Yes" if c.get("risk_flag") else "No",
                "Corroboration Met": "Yes" if c.get("corroboration_met") else "No",
                "Corr. Score": f"{c.get('corroboration_score', 0):.2f}",
                "Corr. Level": c.get("corroboration_level", "none"),
                "First Seen": (c.get("first_seen") or "")[:10],
            })
        st.dataframe(_coerce_df(watch_rows), width="stretch", hide_index=True)
        st.caption(
            "WATCH status requires corroboration_met=True (score ≥ 0.65) and risk_flag=False. "
            "These are research-lane observations only."
        )

    discovered_cands = disc.get("discovered_candidates") or []
    if discovered_cands:
        with st.expander(f"Discovered Candidates ({len(discovered_cands)})", expanded=False):
            disc_rows = []
            for c in discovered_cands:
                if not isinstance(c, dict):
                    continue
                disc_rows.append({
                    "Ticker": c.get("ticker", "?"),
                    "Score": f"{c.get('score', 0):.2f}",
                    "Event Type": c.get("event_type", "unknown"),
                    "Mentions": c.get("mention_count", 0),
                    "Corr. Level": c.get("corroboration_level", "none"),
                })
            st.dataframe(_coerce_df(disc_rows), width="stretch", hide_index=True)

    rejected_cands = disc.get("rejected_candidates") or []
    if rejected_cands:
        with st.expander(f"Rejected Candidates ({len(rejected_cands)})", expanded=False):
            rej_rows = []
            for c in rejected_cands:
                if not isinstance(c, dict):
                    continue
                rej_rows.append({
                    "Ticker": c.get("ticker", "?"),
                    "Rejection Reason": c.get("rejection_reason", "unknown"),
                    "Score": f"{c.get('score', 0):.2f}",
                })
            st.dataframe(_coerce_df(rej_rows), width="stretch", hide_index=True)

    memo_md = disc.get("memo_md", "")
    if memo_md:
        with st.expander("Research Memo", expanded=False):
            st.markdown(memo_md)

    # ------------------------------------------------------------------ #
    # Sandbox Approval Workflow                                           #
    # ------------------------------------------------------------------ #
    st.divider()
    st.subheader("Sandbox Review Decisions")
    st.info(
        "**Discovery approval decisions are sandbox research notes only.** "
        "They are not buy/sell recommendations and do not update the official "
        "watchlist or portfolio. No trade is executed."
    )

    # Show existing approval decisions summary
    approval_summary = disc.get("approval_summary") or {}
    total_decisions = approval_summary.get("total_decisions", 0)
    if total_decisions > 0:
        decision_counts = approval_summary.get("decision_counts") or {}
        summary_cols = st.columns(len(decision_counts) + 1 if decision_counts else 2)
        summary_cols[0].metric("Total Reviews", total_decisions)
        for i, (dec_val, cnt) in enumerate(sorted(decision_counts.items()), start=1):
            if i < len(summary_cols):
                summary_cols[i].metric(dec_val.replace("_", " ").title(), cnt)

        latest_per_symbol = approval_summary.get("latest_per_symbol") or {}
        if latest_per_symbol:
            with st.expander(f"Recorded Reviews ({len(latest_per_symbol)} symbols)", expanded=False):
                review_rows = []
                for sym, d in sorted(latest_per_symbol.items()):
                    if not isinstance(d, dict):
                        continue
                    review_rows.append({
                        "Symbol": sym,
                        "Decision": d.get("decision", "?"),
                        "Reason": (d.get("decision_reason") or "")[:60],
                        "Corr. Level": d.get("corroboration_level", "?"),
                        "Recorded At": (d.get("generated_at") or "")[:19],
                    })
                if review_rows:
                    st.dataframe(_coerce_df(review_rows), width="stretch", hide_index=True)
                st.caption(
                    "Latest decision per symbol (append-only log). "
                    "These are sandbox research notes — not official recommendations."
                )

    # Approval form — one card per WATCH candidate
    reviewable = list(watch_cands)
    if not reviewable:
        st.caption("No WATCH candidates available to review. Run the discovery engine to generate candidates.")
        return

    st.markdown("**Review WATCH Candidates**")
    st.caption(
        "Select a research decision and optional reason for each candidate. "
        "Decisions are recorded as sandbox notes only."
    )

    _DECISION_LABELS: dict[str, str] = {
        ApprovalDecision.APPROVE_FOR_RESEARCH_REVIEW.value: "Approve for Research Review",
        ApprovalDecision.KEEP_WATCHING.value:               "Keep Watching",
        ApprovalDecision.NEEDS_MORE_EVIDENCE.value:         "Needs More Evidence",
        ApprovalDecision.REJECT_CANDIDATE.value:            "Reject Candidate",
    }
    _DECISION_OPTIONS = list(_DECISION_LABELS.keys())

    for cand in reviewable:
        if not isinstance(cand, dict):
            continue
        ticker = cand.get("ticker", "?")
        with st.expander(
            f"{ticker} — score {cand.get('score', 0):.2f} | "
            f"corr: {cand.get('corroboration_level', '?')} ({cand.get('corroboration_score', 0):.2f}) | "
            f"event: {cand.get('event_type', '?')}",
            expanded=False,
        ):
            # Candidate detail
            d1, d2, d3 = st.columns(3)
            d1.markdown(f"**Mentions:** {cand.get('mention_count', 0)}")
            d2.markdown(f"**Sources:** {cand.get('unique_source_count', 0)}")
            d3.markdown(f"**Risk Flag:** {'Yes' if cand.get('risk_flag') else 'No'}")

            d4, d5 = st.columns(2)
            d4.markdown(f"**First Seen:** {(cand.get('first_seen') or '')[:10]}")
            d5.markdown(f"**Last Seen:** {(cand.get('last_seen') or '')[:10]}")

            corr_sources = cand.get("corroboration_sources") or []
            if corr_sources:
                st.markdown(f"**Corroboration Sources:** {', '.join(str(s) for s in corr_sources[:6])}")

            snippets = cand.get("evidence_snippets") or []
            if snippets:
                st.markdown("**Evidence Snippets:**")
                for snip in snippets[:3]:
                    st.caption(f"• {snip}")

            st.caption(
                "This is a sandbox research note. Selecting a decision does not "
                "create a recommendation, modify the watchlist, or execute any trade."
            )

            # Decision form
            sel_key = f"disc_approval_decision_{ticker}"
            reason_key = f"disc_approval_reason_{ticker}"
            btn_key = f"disc_approval_btn_{ticker}"

            selected_label = st.selectbox(
                "Research Decision",
                options=_DECISION_OPTIONS,
                format_func=lambda v: _DECISION_LABELS.get(v, v),
                key=sel_key,
            )
            reason_text = st.text_area(
                "Reason / Notes (optional)",
                key=reason_key,
                height=80,
                placeholder="Optional: explain why you chose this decision...",
            )

            flash_ok_key = f"disc_approval_ok_{ticker}"
            flash_err_key = f"disc_approval_err_{ticker}"
            if st.session_state.pop(flash_ok_key, None):
                st.success(f"Sandbox review decision recorded for {ticker}.")
            _flash_err = st.session_state.pop(flash_err_key, None)
            if _flash_err:
                st.error(f"Failed to record decision for {ticker}: {_flash_err}")

            if st.button("Record sandbox review decision", key=btn_key):
                try:
                    dec = make_approval_decision(
                        symbol=ticker,
                        decision=selected_label,
                        decision_reason=reason_text or "",
                        candidate_status=cand.get("status", "watch"),
                        corroboration_score=float(cand.get("corroboration_score", 0.0)),
                        corroboration_level=cand.get("corroboration_level", "none"),
                        company_name=cand.get("company_name", ""),
                        source_artifact=str(
                            disc.get("artifacts", {}).get("emerging_candidates", "")
                        ),
                        run_id=disc.get("run_id", ""),
                    )
                    record_approval_decision(dec, base_dir=str(ROOT / "outputs"))
                    st.session_state[flash_ok_key] = True
                except Exception as exc:
                    st.session_state[flash_err_key] = str(exc)
                st.rerun()


def _render_weekly_review_tab(bundle: dict) -> None:
    report = bundle.get("weekly_review", {})
    st.subheader("Weekly Review")
    _render_interpretation(
        "This weekly operator report consolidates the same read-only dashboard artifacts into a short written review."
    )

    flash_error = st.session_state.pop("weekly_review_flash_error", None)
    flash_success = st.session_state.pop("weekly_review_flash_success", None)
    if flash_error:
        st.error(flash_error)
    if flash_success:
        st.success(flash_success)

    action_cols = st.columns([1, 1, 1, 2])
    with action_cols[0]:
        if st.button("Regenerate", key="weekly_review_regenerate", width="stretch"):
            try:
                result = generate_weekly_summary(root=ROOT)
            except Exception as exc:
                st.session_state["weekly_review_flash_error"] = f"Weekly report generation failed: {exc}"
            else:
                try:
                    relative_path = result.output_path.relative_to(ROOT)
                except ValueError:
                    relative_path = result.output_path
                st.session_state["weekly_review_flash_success"] = f"Weekly report updated at `{relative_path}`."
            st.rerun()
    with action_cols[1]:
        _render_outputs_action(report.get("output_target"), button_key="weekly_review_open")

    if not report.get("available"):
        st.info("No weekly review has been generated yet.")
        return

    meta_cols = st.columns(3)
    meta_cols[0].metric("Last Updated", report.get("updated_display", "Unknown"))
    meta_cols[1].metric("Age", report.get("age_label", "Unknown"))
    meta_cols[2].metric("Format", "Markdown")

    markdown = report.get("markdown") or "_Weekly report is empty._"
    plain_text = markdown_to_plain_text(markdown)

    st.markdown(markdown)
    with st.expander("Copy / Export", expanded=False):
        st.text_area(
            "Plain Text",
            plain_text,
            height=240,
            key="weekly_review_plain_text",
        )
        export_cols = st.columns(2)
        with export_cols[0]:
            st.download_button(
                "Download Markdown",
                markdown,
                file_name=Path(report.get("path", "weekly_summary.md")).name,
                mime="text/markdown",
                width="stretch",
            )
        with export_cols[1]:
            st.download_button(
                "Download Plain Text",
                plain_text,
                file_name="weekly_summary.txt",
                mime="text/plain",
                width="stretch",
            )


# ============================================================================
# DECISION INTELLIGENCE TABS
# ============================================================================

def _format_decision_queue_rows(raw_rows: list[dict]) -> list[dict]:
    formatted = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        flags = [str(f) for f in (row.get("risk_flags") or []) if str(f).strip()]
        amount = row.get("recommended_amount")
        capital_str = _fmt_usd(amount) if amount is not None else "-"
        try:
            priority_str = f"{float(row.get('priority', 0)):.3f}"
        except (TypeError, ValueError):
            priority_str = "-"
        formatted.append({
            "Action":      str(row.get("decision") or "-"),
            "Symbol":      str(row.get("symbol") or "-"),
            "Source":      str(row.get("source") or "-"),
            "Priority":    priority_str,
            "Urgency":     str(row.get("urgency") or "-"),
            "Reason":      str(row.get("reason") or "No rationale provided.")[:100],
            "Risk Flags":  ", ".join(flags) if flags else "-",
            "Capital":     capital_str,
        })
    return formatted


def _render_ai_insight_cards(bundle: dict) -> None:
    data = bundle.get("decision_explanations") or {}
    cards = _get_insight_cards(data)

    st.markdown("### AI Insights")

    if not cards:
        msg = data.get("summary_line") or "No AI explanations available."
        st.caption(msg)
        return

    for row in cards:
        action = row.get("action") or "UNKNOWN"
        symbol = row.get("symbol") or "UNKNOWN"
        validation = str(row.get("ai_validation") or "neutral").lower().strip()
        badge = _ai_validation_badge(validation)
        if validation == "boost":
            badge_md = f":green[{badge}]"
        elif validation == "caution":
            badge_md = f":orange[{badge}]"
        else:
            badge_md = badge

        st.markdown(f"**{action} {symbol}** | {badge_md}")
        st.write(row.get("concise_explanation") or "No explanation available.")

        risks = (row.get("risks") or [])[:3]
        if risks:
            st.caption(f"Risks: {', '.join(risks)}")

        watch_items = (row.get("what_to_watch_next") or [])[:3]
        if watch_items:
            st.caption("Watch next:")
            for item in watch_items:
                st.caption(f"  - {item}")

        st.markdown("---")


def _render_decision_performance_attribution(bundle: dict) -> None:
    data = bundle.get("decision_performance_attribution") or {}
    st.markdown("### Performance Attribution")

    if not data.get("available"):
        msg = data.get("summary_line") or "No performance attribution data available."
        if data.get("insufficient_data"):
            st.caption(f"Insufficient data — {msg}")
        else:
            st.caption(msg)
        return

    total = data.get("total_decisions", 0)
    resolved = data.get("resolved_decisions", 0)
    hit_rate = data.get("hit_rate")
    avg_return = data.get("avg_return")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Decisions", total)
    m2.metric("Resolved", resolved)
    m3.metric("Hit Rate", f"{hit_rate:.0%}" if hit_rate is not None else "—")
    m4.metric("Avg Return", f"{avg_return:+.2%}" if avg_return is not None else "—")

    def _breakdown_df(d: dict) -> list[dict]:
        rows = []
        for label, stats in (d or {}).items():
            if stats.get("total", 0) == 0:
                continue
            hr = stats.get("hit_rate")
            ar = stats.get("avg_return")
            rows.append({
                "Label": label.replace("_", " ").title(),
                "Total": stats.get("total", 0),
                "Resolved": stats.get("resolved", 0),
                "Hit Rate": f"{hr:.0%}" if hr is not None else "—",
                "Avg Return": f"{ar:+.2%}" if ar is not None else "—",
            })
        return rows

    with st.expander("Breakdown Tables", expanded=False):
        by_dec = _breakdown_df(data.get("by_decision") or {})
        if by_dec:
            st.markdown("**By Decision Type**")
            st.dataframe(_coerce_df(by_dec, columns=["Label", "Total", "Resolved", "Hit Rate", "Avg Return"]),
                         hide_index=True, width="stretch")

        by_strat = _breakdown_df(data.get("by_strategy") or {})
        if by_strat:
            st.markdown("**By Strategy**")
            st.dataframe(_coerce_df(by_strat, columns=["Label", "Total", "Resolved", "Hit Rate", "Avg Return"]),
                         hide_index=True, width="stretch")

        by_val = _breakdown_df(data.get("by_validation_status") or {})
        if by_val:
            st.markdown("**By Validation Status**")
            st.dataframe(_coerce_df(by_val, columns=["Label", "Total", "Resolved", "Hit Rate", "Avg Return"]),
                         hide_index=True, width="stretch")

        by_triage = _breakdown_df(data.get("by_triage_bucket") or {})
        if by_triage:
            st.markdown("**By Triage Bucket**")
            st.dataframe(_coerce_df(by_triage, columns=["Label", "Total", "Resolved", "Hit Rate", "Avg Return"]),
                         hide_index=True, width="stretch")

    best = data.get("best_decision")
    worst = data.get("worst_decision")
    if best or worst:
        st.markdown("**Notable Decisions**")
        if best:
            ret = best.get("return_pct")
            st.success(
                f"Best: **{best.get('decision')} {best.get('symbol')}** "
                f"on {best.get('date')} — {f'{ret:+.2%}' if ret is not None else '—'} "
                f"({best.get('validation_status', '—')})"
            )
        if worst:
            ret = worst.get("return_pct")
            st.warning(
                f"Worst: **{worst.get('decision')} {worst.get('symbol')}** "
                f"on {worst.get('date')} — {f'{ret:+.2%}' if ret is not None else '—'} "
                f"({worst.get('validation_status', '—')})"
            )


def _render_system_confidence_section(bundle: dict) -> None:
    data = bundle.get("confidence_calibration") or {}

    st.markdown("### System Confidence")

    if not data.get("available"):
        msg = data.get("summary_line") or "Confidence calibration artifact not available yet."
        st.caption(msg)
        return

    if data.get("insufficient_data"):
        st.caption(data.get("summary_line") or "Insufficient resolved data for calibration.")
        return

    total = data.get("total_resolved", 0)
    overall_hr = data.get("overall_hit_rate")
    overall_ret = data.get("overall_avg_return")

    col1, col2, col3 = st.columns(3)
    col1.metric("Resolved Decisions", total)
    col2.metric("Overall Hit Rate", f"{overall_hr:.0%}" if overall_hr is not None else "—")
    col3.metric("Avg Return", f"{overall_ret:+.2%}" if overall_ret is not None else "—")

    st.caption(
        "Retrospective calibration — observe-only. "
        "Source: `outputs/policy/confidence_calibration.json`."
    )

    # Confidence bucket table
    conf_buckets = data.get("confidence_buckets") or {}
    bucket_rows = []
    for key in ("low", "medium", "high"):
        stats = conf_buckets.get(key) or {}
        if stats.get("count", 0) > 0:
            hr = stats.get("hit_rate")
            ar = stats.get("avg_return")
            bucket_rows.append({
                "Confidence": key.capitalize(),
                "Count": stats.get("count", 0),
                "Hit Rate": f"{hr:.0%}" if hr is not None else "—",
                "Avg Return": f"{ar:+.2%}" if ar is not None else "—",
            })
    if bucket_rows:
        st.markdown("**Confidence Buckets**")
        st.dataframe(
            _coerce_df(bucket_rows, columns=["Confidence", "Count", "Hit Rate", "Avg Return"]),
            hide_index=True,
            width="stretch",
        )

    # Validation status table
    val_analysis = data.get("validation_analysis") or {}
    val_rows = []
    for key in sorted(val_analysis):
        stats = val_analysis[key] or {}
        if stats.get("count", 0) > 0:
            hr = stats.get("hit_rate")
            ar = stats.get("avg_return")
            val_rows.append({
                "Validation Status": key.replace("_", " ").title(),
                "Count": stats.get("count", 0),
                "Hit Rate": f"{hr:.0%}" if hr is not None else "—",
                "Avg Return": f"{ar:+.2%}" if ar is not None else "—",
            })
    if val_rows:
        st.markdown("**Validation Status**")
        st.dataframe(
            _coerce_df(
                val_rows,
                columns=["Validation Status", "Count", "Hit Rate", "Avg Return"],
            ),
            hide_index=True,
            width="stretch",
        )

    # Key insights
    insights = (data.get("insights") or [])[:3]
    if insights:
        st.markdown("**Key Insights**")
        for insight in insights:
            st.info(insight)


def _render_decision_performance_section(bundle: dict) -> None:
    data = bundle.get("decision_outcome_summary") or {}

    st.markdown("### Decision Performance")

    if not data.get("available"):
        st.caption(data.get("summary_line") or "No decision performance data yet.")
        return

    total = data.get("total_decisions", 0)
    resolved = data.get("resolved", 0)
    hit_rate = data.get("hit_rate")
    avg_return = data.get("avg_return_pct")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Tracked", total)
    col2.metric("Resolved", resolved)
    col3.metric("Hit Rate", f"{hit_rate:.0%}" if hit_rate is not None else "—")
    col4.metric("Avg Return", f"{avg_return:+.2%}" if avg_return is not None else "—")

    st.caption(
        "Observe-only. Tracks historical decision accuracy. "
        "Source: `outputs/policy/decision_outcome_summary.json`."
    )

    by_decision = data.get("by_decision") or {}
    if by_decision:
        st.markdown("**By Decision Type**")
        table_rows = []
        for dec, stats in sorted(by_decision.items()):
            hr = stats.get("hit_rate")
            ar = stats.get("avg_return_pct")
            table_rows.append({
                "Decision": dec,
                "Count": stats.get("count", 0),
                "Resolved": stats.get("resolved", 0),
                "Hit Rate": f"{hr:.0%}" if hr is not None else "—",
                "Avg Return": f"{ar:+.2%}" if ar is not None else "—",
            })
        st.dataframe(
            _coerce_df(table_rows, columns=["Decision", "Count", "Resolved", "Hit Rate", "Avg Return"]),
            hide_index=True,
            width="stretch",
        )

    last_10 = data.get("last_10_resolved") or []
    if last_10:
        with st.expander("Last 10 Resolved Decisions", expanded=False):
            formatted = []
            for row in last_10[:10]:
                rp = row.get("return_pct")
                dc = row.get("direction_correct")
                formatted.append({
                    "Date": row.get("date", "—"),
                    "Symbol": row.get("symbol", "—"),
                    "Decision": row.get("decision", "—"),
                    "Days": row.get("days_elapsed", "—"),
                    "Return": f"{rp:+.2%}" if rp is not None else "—",
                    "Correct": "✓" if dc is True else ("✗" if dc is False else "—"),
                    "Validation": row.get("validation_status", "—"),
                })
            st.dataframe(
                _coerce_df(
                    formatted,
                    columns=["Date", "Symbol", "Decision", "Days", "Return", "Correct", "Validation"],
                ),
                hide_index=True,
                width="stretch",
            )


_TRIAGE_BUCKET_BADGE: dict[str, str] = {
    "critical_action": ":red[🔴 critical]",
    "action_candidate": ":orange[🟠 action candidate]",
    "monitor": ":blue[🔵 monitor]",
    "ignore_for_now": ":gray[⚪ ignore]",
}

_TRIAGE_SEVERITY_BADGE: dict[str, str] = {
    "critical": ":red[critical]",
    "high": ":orange[high]",
    "medium": ":blue[medium]",
    "low": ":gray[low]",
}


def _render_decision_triage_section(bundle: dict) -> None:
    data = bundle.get("decision_triage") or {}

    st.markdown("### Decision Triage")

    if not data.get("available"):
        st.caption(data.get("summary_line") or "Decision triage artifact not available yet.")
        return

    counts = data.get("bucket_counts") or {}
    critical_n = counts.get("critical_action", 0)
    action_n = counts.get("action_candidate", 0)
    monitor_n = counts.get("monitor", 0)
    ignore_n = counts.get("ignore_for_now", 0)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Critical Action", critical_n)
    col2.metric("Action Candidate", action_n)
    col3.metric("Monitor", monitor_n)
    col4.metric("Ignore For Now", ignore_n)

    st.caption(
        f"{data.get('total_decisions', 0)} decisions triaged. "
        "Observe-only — no recomputation occurs here. "
        "Source: `outputs/latest/decision_triage.json`."
    )

    # Top 3 actions
    top_actions = (data.get("top_actions") or [])[:3]
    if top_actions:
        st.markdown("**Top Actions Today**")
        for row in top_actions:
            bucket = str(row.get("triage_bucket") or "monitor").lower()
            severity = str(row.get("severity") or "low").lower()
            bucket_badge = _TRIAGE_BUCKET_BADGE.get(bucket, f":gray[{bucket}]")
            severity_badge = _TRIAGE_SEVERITY_BADGE.get(severity, f":gray[{severity}]")
            st.markdown(
                f"**{row.get('decision')} {row.get('symbol')}** | "
                f"{bucket_badge} | severity: {severity_badge}"
            )
            st.write(row.get("reason") or "—")
            next_act = row.get("next_action") or ""
            if next_act:
                st.caption(f"Next: {next_act}")
            watch = row.get("watch_next") or []
            if watch:
                st.caption(f"Watch next: {'; '.join(str(w) for w in watch)}")
            st.markdown("---")

    buckets_data = data.get("buckets") or {}

    # Grouped sections: critical + action as tabs if both present, else flat
    critical_rows = buckets_data.get("critical_action") or []
    action_rows = buckets_data.get("action_candidate") or []
    monitor_rows = buckets_data.get("monitor") or []

    tab_labels = []
    if critical_rows:
        tab_labels.append(f"Critical ({len(critical_rows)})")
    if action_rows:
        tab_labels.append(f"Action ({len(action_rows)})")
    if monitor_rows:
        tab_labels.append(f"Monitor ({len(monitor_rows)})")

    if tab_labels:
        tabs = st.tabs(tab_labels)
        tab_idx = 0

        if critical_rows:
            with tabs[tab_idx]:
                for row in critical_rows:
                    sev = str(row.get("severity") or "critical").lower()
                    st.markdown(
                        f"**{row.get('decision')} {row.get('symbol')}** | "
                        f"rank #{row.get('triage_rank')} | "
                        f"{_TRIAGE_SEVERITY_BADGE.get(sev, sev)}"
                    )
                    st.write(row.get("reason") or "—")
                    st.caption(f"Next: {row.get('next_action') or '—'}")
                    v_status = row.get("validation_status") or "—"
                    priority = row.get("priority")
                    p_str = f"{priority:.3f}" if priority is not None else "—"
                    st.caption(f"Validation: {v_status} | Priority: {p_str}")
                    rf = row.get("risk_flags") or []
                    if rf:
                        st.caption(f"Risk flags: {', '.join(str(f) for f in rf)}")
                    st.markdown("---")
            tab_idx += 1

        if action_rows:
            with tabs[tab_idx]:
                for row in action_rows:
                    sev = str(row.get("severity") or "high").lower()
                    st.markdown(
                        f"**{row.get('decision')} {row.get('symbol')}** | "
                        f"rank #{row.get('triage_rank')} | "
                        f"{_TRIAGE_SEVERITY_BADGE.get(sev, sev)}"
                    )
                    st.write(row.get("reason") or "—")
                    st.caption(f"Next: {row.get('next_action') or '—'}")
                    v_status = row.get("validation_status") or "—"
                    priority = row.get("priority")
                    p_str = f"{priority:.3f}" if priority is not None else "—"
                    st.caption(f"Validation: {v_status} | Priority: {p_str}")
                    st.markdown("---")
            tab_idx += 1

        if monitor_rows:
            with tabs[tab_idx]:
                table_rows = []
                for row in monitor_rows:
                    priority = row.get("priority")
                    table_rows.append({
                        "Decision": row.get("decision", "—"),
                        "Symbol": row.get("symbol", "—"),
                        "Reason": (row.get("reason") or "")[:80],
                        "Validation": row.get("validation_status", "—"),
                        "Priority": f"{priority:.3f}" if priority is not None else "—",
                    })
                st.dataframe(
                    _coerce_df(
                        table_rows,
                        columns=["Decision", "Symbol", "Reason", "Validation", "Priority"],
                    ),
                    hide_index=True,
                    width="stretch",
                )


_VALIDATION_STATUS_BADGE: dict[str, str] = {
    "aligned": ":green[✓ aligned]",
    "caution": ":orange[⚠ caution]",
    "contradiction": ":red[✗ contradiction]",
    "insufficient_context": ":gray[? insufficient context]",
}


def _render_ai_validation_section(bundle: dict) -> None:
    data = bundle.get("ai_decision_validation") or {}

    st.markdown("### AI Validation")

    if not data.get("available"):
        st.caption(data.get("summary_line") or "AI validation artifact not available yet.")
        return

    total = data.get("total_validated", 0)
    aligned = data.get("aligned_count", 0)
    caution = data.get("caution_count", 0)
    contradiction = data.get("contradiction_count", 0)
    insufficient = data.get("insufficient_context_count", 0)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Aligned", aligned)
    col2.metric("Caution", caution)
    col3.metric("Contradiction", contradiction)
    col4.metric("Insufficient", insufficient)

    st.caption(
        f"Validated {total} decision(s). "
        f"AI used: {data.get('ai_used', False)}. "
        "Observe-only — no recomputation occurs here."
    )

    validations = (data.get("validations") or [])[:5]
    if not validations:
        return

    for record in validations:
        status = str(record.get("validation_status") or "caution").lower().strip()
        badge_md = _VALIDATION_STATUS_BADGE.get(status, f":gray[{status}]")
        decision = record.get("decision") or "-"
        symbol = record.get("symbol") or "-"
        st.markdown(f"**{decision} {symbol}** | {badge_md}")
        st.write(record.get("plain_english_summary") or "No summary available.")
        contradictions = record.get("contradictions") or []
        if contradictions:
            st.caption(f"Conflict: {'; '.join(contradictions)}")
        watch = record.get("watch_next") or []
        if watch:
            st.caption(f"Watch next: {'; '.join(watch)}")
        rule = record.get("rule_alignment") or ""
        if rule:
            st.caption(f"Rule: {rule}")
        st.markdown("---")


def _render_decision_brief_summary(bundle: dict) -> None:
    brief = bundle.get("decision_brief") or {}

    st.info("Observe-only decision plan. No trades are executed.")
    st.markdown("### Decision Summary")
    st.caption("Compact brief derived from `decision_plan.json` and system summary artifacts. Read-only.")

    if not brief.get("available"):
        st.warning(brief.get("summary_line") or "Decision plan unavailable.")
        st.caption(f"Expected at: `{brief.get('path', 'outputs/latest/decision_plan.json')}`")
        return

    top_decisions = brief.get("top_decisions") or []
    if not top_decisions:
        st.info("No actions required. Decision plan is present but contains no ranked decisions.")
        return

    st.markdown("**Top Insight**")
    st.write(brief.get("top_insight") or "No top insight available.")

    st.markdown("**Top Decisions** *(max 5)*")
    for idx, row in enumerate(top_decisions[:5], 1):
        try:
            pri = f"{float(row.get('priority', 0.0)):.3f}"
        except (TypeError, ValueError):
            pri = "-"
        header = (
            f"{idx}. **{row.get('decision', '-')} {row.get('symbol', '-')}**"
            f" | {row.get('source', '-')} | {row.get('urgency', '-')} | pri {pri}"
        )
        st.markdown(header)
        st.caption(f"  {row.get('compact_reason') or _compact_decision_reason(row)}")

    st.markdown("**Insight Cards**")
    render_insight_cards([row.get("raw") or row for row in top_decisions[:5]])

    capital = brief.get("capital_actions") or {}
    st.markdown("**Capital Actions**")
    capital_line = (
        f"SELL={int(capital.get('sell', 0))}, "
        f"SCALE={int(capital.get('scale', 0))}, "
        f"BUY={int(capital.get('buy', 0))}"
    )
    if capital.get("total_recommended_capital") is not None:
        capital_line += f" | Total: {_fmt_usd(capital.get('total_recommended_capital'))}"
    st.write(capital_line)

    st.markdown("**Risk Focus** *(max 3)*")
    risk_items = (brief.get("risk_focus") or [])[:3]
    if risk_items:
        for item in risk_items:
            st.markdown(f"- {item}")
    else:
        st.caption("No risk items flagged.")

    st.markdown("**What Changed** *(max 3)*")
    change_items = (brief.get("what_changed") or [])[:3]
    if change_items:
        for item in change_items:
            st.markdown(f"- {item}")
    else:
        st.caption("No changes recorded.")

    health_items = (brief.get("system_data_health") or [])[:3]
    if health_items:
        st.markdown("**System / Data Health**")
        for item in health_items:
            st.markdown(f"- {item}")

    st.divider()
    _render_ai_insight_cards(bundle)
    st.divider()
    with st.expander("Full Decision Plan Queue", expanded=False):
        full_rows = brief.get("full_decisions") or []
        if not full_rows:
            st.caption("No decision-plan rows available.")
        else:
            formatted = _format_decision_queue_rows(full_rows)
            st.dataframe(
                _coerce_df(
                    formatted,
                    columns=["Action", "Symbol", "Source", "Priority", "Urgency", "Reason", "Risk Flags", "Capital"],
                ),
                width="stretch",
                hide_index=True,
            )
            st.caption(f"{len(full_rows)} total decision(s) in the plan.")


def _render_decision_center_tab(bundle: dict, mc: dict) -> None:
    st.subheader("Portfolio Decision Center")
    _render_mc_freshness(mc)
    _render_interpretation(
        "Actionable decisions from the last run. Shows WHY each action was recommended — "
        "score, confidence, strategy type, and full rationale."
    )
    _render_decision_brief_summary(bundle)
    st.divider()

    decision_layer = mc.get("decision_layer") or {}

    if not decision_layer.get("available"):
        st.info(
            "No portfolio decision data found. "
            "Run a Daily/Weekly/Monthly analysis with market_coverage enabled."
        )
        st.caption("Expected at: `outputs/latest/market_opportunities.json`")
        return

    actions = decision_layer.get("actions") or []
    if not actions:
        st.info("Decision layer ran but produced no actions.")
        st.caption(decision_layer.get("summary_line") or "")
        return

    buy_actions  = [a for a in actions if a.get("action", "").upper() in {"BUY", "PROMOTE_TO_PORTFOLIO"}]
    sell_actions = [a for a in actions if a.get("action", "").upper() in {"SELL", "TRIM"}]
    watch_actions = [a for a in actions if a.get("action", "").upper() == "ADD_TO_WATCHLIST"]
    hold_actions = [a for a in actions if a.get("action", "").upper() == "HOLD"]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Buy / Promote", len(buy_actions))
    m2.metric("Sell / Trim", len(sell_actions))
    m3.metric("Watch", len(watch_actions))
    m4.metric("Hold", len(hold_actions))
    st.caption(decision_layer.get("summary_line") or "")

    col_f1, col_f2, col_f3, col_f4 = st.columns(4)
    all_action_types = sorted({a.get("action", "UNKNOWN") for a in actions})
    default_types = [t for t in all_action_types if t.upper() not in {"ADD_TO_WATCHLIST", "HOLD"}] or all_action_types
    with col_f1:
        selected_types = st.multiselect(
            "Filter by action", all_action_types, default=default_types, key="dc_action_filter"
        )
    all_strategies = sorted({a.get("strategy_type") or "unknown" for a in actions})
    with col_f2:
        selected_strategies = st.multiselect(
            "Filter by strategy", all_strategies, default=all_strategies, key="dc_strategy_filter"
        )
    with col_f3:
        high_conf_only = st.checkbox("High confidence only (≥75%)", key="dc_high_conf_filter")
    with col_f4:
        exits_only = st.checkbox("Exits only", key="dc_exits_only_filter")

    filtered = [
        a for a in actions
        if a.get("action", "") in selected_types
        and (a.get("strategy_type") or "unknown") in selected_strategies
        and (not high_conf_only or _coerce_num(a.get("confidence"), 0) >= 0.75)
        and (not exits_only or a.get("action", "").upper() in {"SELL", "TRIM"})
    ]

    if not filtered:
        st.info("No actions match the current filters.")
        return

    rows = []
    for a in filtered:
        conf  = a.get("confidence")
        score = a.get("score")
        rationale = a.get("rationale") or []
        rows.append({
            "Priority":   _action_priority(a),
            "Symbol":     a.get("symbol", "?"),
            "Action":     a.get("action", "?"),
            "Strategy":   a.get("strategy_type") or "—",
            "Score":      f"{score:.1f}" if score is not None else "—",
            "Confidence": f"{conf * 100:.0f}%" if conf is not None else "—",
            "Key Reason": (rationale[0] if rationale else "—")[:80],
            "Allocation": _fmt_pct(a.get("suggested_allocation_pct")) if a.get("suggested_allocation_pct") else "—",
        })
    high_priority = [r for r in rows if r["Priority"] == "HIGH"]
    if high_priority:
        st.markdown(
            "**High priority:** "
            + "  ".join(
                _badge(f"{r['Action']} {r['Symbol']}", _action_tone(r["Action"]))
                for r in high_priority
            ),
            unsafe_allow_html=True,
        )
    st.dataframe(_coerce_df(rows), width="stretch", hide_index=True)

    st.subheader("Decision Detail")
    sym_options = [a.get("symbol", "?") for a in filtered]
    selected_sym = st.selectbox("Inspect decision for", sym_options, key="dc_sym_select")
    sel_action = next((a for a in filtered if a.get("symbol") == selected_sym), None)

    if sel_action:
        action_type = sel_action.get("action", "UNKNOWN")
        tone = _action_tone(action_type)
        col_a, col_b, col_c = st.columns(3)
        col_a.markdown(
            _badge(action_type, tone) + " " + _badge(sel_action.get("strategy_type") or "unclassified", "neutral"),
            unsafe_allow_html=True,
        )
        score = sel_action.get("score")
        confidence = sel_action.get("confidence")
        col_b.metric("Score", f"{score:.1f}" if score is not None else "N/A")
        col_c.metric("Confidence", f"{confidence * 100:.0f}%" if confidence is not None else "N/A")

        rationale = sel_action.get("rationale") or []
        if rationale:
            st.markdown("**Why this decision?**")
            for r in rationale:
                st.markdown(f"- {r}")

        alloc_pct = sel_action.get("suggested_allocation_pct")
        alloc_amt = sel_action.get("suggested_allocation_amount")
        if alloc_pct is not None or alloc_amt:
            parts = []
            if alloc_pct is not None:
                parts.append(f"{alloc_pct * 100:.1f}%")
            if alloc_amt:
                parts.append(f"~${alloc_amt:,.0f}")
            st.caption("Suggested allocation: " + " / ".join(parts))

        related = sel_action.get("related_symbol")
        if related:
            st.caption(f"Compared against: {related}")

        exit_plan = sel_action.get("exit_plan") or {}
        if exit_plan:
            with st.expander("Exit Plan", expanded=False):
                for k, v in exit_plan.items():
                    if v:
                        st.markdown(f"**{k.replace('_', ' ').title()}:** {v}")


def _render_opportunities_tab(bundle: dict, mc: dict) -> None:
    st.subheader("Opportunity Ranking")
    _render_mc_freshness(mc)
    _render_interpretation(
        "Top-ranked market candidates from the last scan. Promoted items cleared the score threshold. "
        "Deferred items are in the watchlist but below the actionable bar."
    )

    promoted = mc.get("promoted") or []
    snapshot_rows = bundle.get("portfolio_view", {}).get("rows") or []

    tab_promoted, tab_deferred, tab_events = st.tabs(["Promoted Candidates", "Deferred / Watchlist", "Market Events"])

    with tab_promoted:
        if not promoted:
            st.info(
                "No promoted candidates in the latest run. "
                "Run a market coverage scan to populate this tab — candidates appear once they "
                "clear the score and confidence thresholds."
            )
        else:
            promo_rows = [
                {
                    "Rank":           p.get("rank", "?"),
                    "Symbol":         p.get("symbol", "?"),
                    "Score":          f"{p.get('score', 0):.1f}",
                    "Label":          p.get("label", "—"),
                    "Theme Support":  f"{p.get('theme_support', 0) * 100:.0f}%" if p.get("theme_support") is not None else "—",
                    "Events":         ", ".join(p.get("events") or []) or "—",
                    "Portfolio Hint": (p.get("portfolio_context") or {}).get("action_hint") or "—",
                }
                for p in promoted
            ]
            st.dataframe(_coerce_df(promo_rows), width="stretch", hide_index=True)

            st.subheader("Why was it promoted?")
            sym_opts = [p.get("symbol", "?") for p in promoted]
            sel = st.selectbox("Inspect candidate", sym_opts, key="opp_promo_sel")
            sel_promo = next((p for p in promoted if p.get("symbol") == sel), None)
            if sel_promo:
                c1, c2, c3 = st.columns(3)
                c1.metric("Score", f"{sel_promo.get('score', 0):.1f}")
                c2.metric("Rank", f"#{sel_promo.get('rank', '?')}")
                c3.metric("Label", sel_promo.get("label", "—"))

                events = sel_promo.get("events") or []
                if events:
                    st.markdown("**Events:** " + "".join(_badge(e, "neutral") for e in events), unsafe_allow_html=True)

                reasons = sel_promo.get("reasons") or []
                if reasons:
                    st.markdown("**Scoring factors:**")
                    for r in reasons:
                        st.markdown(f"- {r}")

                portfolio_ctx = sel_promo.get("portfolio_context") or {}
                if portfolio_ctx.get("action_hint"):
                    st.info(f"Portfolio context: {portfolio_ctx['action_hint']}")

    with tab_deferred:
        deferred = [
            r for r in snapshot_rows
            if str(r.get("conviction_band", "")).lower() in {"defer", "suppressed", "observe"}
        ]
        if not deferred:
            st.info("No deferred or suppressed signals in the latest portfolio construction data.")
        else:
            st.caption(f"{len(deferred)} symbols below actionable conviction threshold.")
            defer_rows = [
                {
                    "Symbol":     r.get("ticker", "?"),
                    "Band":       r.get("conviction_band", "—"),
                    "Conviction": _fmt_ratio_pct(r.get("conviction_score")),
                    "Sector":     r.get("sector", "—"),
                    "Alloc":      _fmt_pct(r.get("suggested_allocation")) if r.get("suggested_allocation") else "$0",
                    "Why Deferred": "Score below actionable threshold",
                }
                for r in sorted(deferred, key=lambda x: x.get("conviction_score", 0))
            ]
            st.dataframe(_coerce_df(defer_rows), width="stretch", hide_index=True)
            st.caption(
                "These symbols passed initial screening but did not clear the conviction "
                "threshold for allocation. Re-run to check for updated scores."
            )

    with tab_events:
        event_summary = mc.get("event_summary") or {}
        if not event_summary:
            st.info("No market events detected in the latest run.")
        else:
            ev_rows = [
                {"Event": k.replace("_", " ").title(), "Count": v}
                for k, v in sorted(event_summary.items(), key=lambda x: x[1], reverse=True)
            ]
            st.dataframe(_coerce_df(ev_rows), width="stretch", hide_index=True)


def _render_portfolio_vs_market_tab(bundle: dict, mc: dict) -> None:
    st.subheader("Portfolio vs Market Opportunities")
    _render_mc_freshness(mc)
    _render_interpretation(
        "Compare watchlist conviction against top external opportunities. "
        "Strong external candidates with higher scores may justify rotation."
    )

    promoted = mc.get("promoted") or []
    portfolio_rows = bundle.get("portfolio_view", {}).get("rows") or []

    if not portfolio_rows and not promoted:
        st.info("No portfolio or opportunity data available. Run the system to populate.")
        return

    left_col, right_col = st.columns(2)

    with left_col:
        st.markdown("### Watchlist Holdings")
        if not portfolio_rows:
            st.info("No portfolio construction data.")
        else:
            actionable = [r for r in portfolio_rows if str(r.get("conviction_band", "")).lower() not in {"defer", "suppressed"}]
            held_rows = [
                {
                    "Symbol":     r.get("ticker", "?"),
                    "Conviction": _fmt_ratio_pct(r.get("conviction_score")),
                    "Band":       r.get("conviction_band", "—"),
                    "Alloc":      _fmt_pct(r.get("suggested_allocation")) if r.get("suggested_allocation") else "—",
                    "Sector":     r.get("sector", "—"),
                }
                for r in sorted(portfolio_rows, key=lambda x: x.get("conviction_score", 0), reverse=True)
            ]
            st.dataframe(_coerce_df(held_rows), width="stretch", hide_index=True)
            st.caption(f"{len(actionable)} actionable  |  {len(portfolio_rows) - len(actionable)} deferred")

    with right_col:
        st.markdown("### External Opportunities (Promoted)")
        if not promoted:
            st.info("No promoted opportunities. Enable market_coverage and re-run.")
        else:
            promo_rows = [
                {
                    "Symbol": p.get("symbol", "?"),
                    "Score":  f"{p.get('score', 0):.1f}",
                    "Rank":   f"#{p.get('rank', '?')}",
                    "Label":  p.get("label", "—"),
                    "Events": ", ".join(p.get("events") or [])[:40] or "—",
                }
                for p in sorted(promoted, key=lambda x: x.get("score", 0), reverse=True)
            ]
            st.dataframe(_coerce_df(promo_rows), width="stretch", hide_index=True)

    if promoted and portfolio_rows:
        st.subheader("Rotation Potential")
        portfolio_symbols = {r.get("ticker", "").upper() for r in portfolio_rows}
        rotation_candidates = [
            p for p in promoted
            if p.get("symbol", "").upper() not in portfolio_symbols and p.get("score", 0) >= 40
        ]
        if rotation_candidates:
            rot_rows = [
                {
                    "Symbol": p.get("symbol", "?"),
                    "Score":  f"{p.get('score', 0):.1f}",
                    "Label":  p.get("label", "—"),
                    "Events": ", ".join(p.get("events") or [])[:40] or "—",
                    "In Portfolio": "No",
                }
                for p in rotation_candidates[:10]
            ]
            st.dataframe(_coerce_df(rot_rows), width="stretch", hide_index=True)
            st.caption(
                f"{len(rotation_candidates)} external candidates not currently in watchlist — "
                "review for potential rotation."
            )
        else:
            st.success("No strong rotation candidates identified outside current portfolio.")

        # Rotation spotlight: weakest holding vs strongest external candidate
        not_in_portfolio = [
            p for p in promoted
            if p.get("symbol", "").upper() not in portfolio_symbols
        ]
        if portfolio_rows and not_in_portfolio:
            weakest  = min(portfolio_rows, key=lambda x: x.get("conviction_score", 1.0))
            strongest = max(not_in_portfolio, key=lambda x: x.get("score", 0))
            st.subheader("Rotation Spotlight")
            rs_c1, rs_c2 = st.columns(2)
            with rs_c1:
                st.markdown("**Weakest Current Holding**")
                band = weakest.get("conviction_band", "—")
                st.markdown(
                    _badge(weakest.get("ticker", "?"), _conviction_band_tone(band))
                    + f"  conviction {_fmt_ratio_pct(weakest.get('conviction_score'))}  ·  band: {band}",
                    unsafe_allow_html=True,
                )
            with rs_c2:
                st.markdown("**Strongest External Candidate**")
                st.markdown(
                    _badge(strongest.get("symbol", "?"), "good")
                    + f"  score {strongest.get('score', 0):.1f}  ·  {strongest.get('label', '—')}",
                    unsafe_allow_html=True,
                )
            ext_score  = strongest.get("score", 0)
            hold_conv  = weakest.get("conviction_score", 1.0)
            if ext_score >= 65 and hold_conv < 0.35:
                st.warning(
                    f"Rotation may be justified: {strongest.get('symbol','?')} (score {ext_score:.0f}) "
                    f"vs {weakest.get('ticker','?')} (conviction {hold_conv:.2f})"
                )
            elif ext_score >= 55 and hold_conv < 0.50:
                st.info(
                    f"Worth monitoring: {strongest.get('symbol','?')} (score {ext_score:.0f}) "
                    f"vs {weakest.get('ticker','?')} (conviction {hold_conv:.2f})"
                )
            else:
                st.success("Current holdings compare favorably against available external opportunities.")

    portfolio_review = mc.get("portfolio_review") or {}
    if portfolio_review.get("available"):
        with st.expander("Portfolio Review Summary", expanded=False):
            st.markdown(f"- {portfolio_review.get('summary_line', 'Portfolio review available.')}")
            rc1, rc2, rc3 = st.columns(3)
            rc1.metric("Confirmations", portfolio_review.get("existing_holding_confirmations", 0))
            rc2.metric("Scanner Confirmed", portfolio_review.get("scanner_confirmation_count", 0))
            rc3.metric("New Rotation Candidates", portfolio_review.get("new_rotation_candidates", 0))

    regime_commentary = bundle.get("portfolio_view", {}).get("regime_commentary") or ""
    if regime_commentary:
        st.caption(f"Regime context: {regime_commentary}")


def _render_strategy_breakdown_tab(bundle: dict, mc: dict, perf_summary: dict | None = None) -> None:
    st.subheader("Strategy Breakdown")
    _render_mc_freshness(mc)
    _render_interpretation(
        "Decisions split by strategy type. Compounders are long-term quality holds "
        "(wider exits). Momentum trades are event-driven setups (tighter exits)."
    )

    decision_layer = mc.get("decision_layer") or {}
    actions = decision_layer.get("actions") or []

    compounders   = [a for a in actions if str(a.get("strategy_type") or "").lower() == "compounder"]
    momentum      = [a for a in actions if str(a.get("strategy_type") or "").lower() == "momentum"]
    unclassified  = [a for a in actions if not a.get("strategy_type")]

    m1, m2, m3 = st.columns(3)
    m1.metric("Compounders", len(compounders))
    m2.metric("Momentum", len(momentum))
    m3.metric("Unclassified", len(unclassified))

    tab_comp, tab_mom, tab_sector, tab_perf = st.tabs(
        ["Compounders", "Momentum", "Sector View", "Strategy Performance"]
    )

    def _strategy_df(items: list) -> pd.DataFrame:
        return _coerce_df(
            [
                {
                    "Symbol":     a.get("symbol", "?"),
                    "Action":     a.get("action", "?"),
                    "Score":      f"{a.get('score', 0):.1f}" if a.get("score") is not None else "—",
                    "Confidence": f"{a.get('confidence', 0) * 100:.0f}%" if a.get("confidence") is not None else "—",
                    "Allocation": _fmt_pct(a.get("suggested_allocation_pct")) if a.get("suggested_allocation_pct") else "—",
                    "Key Reason": (a.get("rationale") or ["—"])[0][:80],
                }
                for a in sorted(items, key=lambda x: x.get("score") or 0, reverse=True)
            ]
        )

    with tab_comp:
        if not compounders:
            st.info(
                "No compounder decisions in the latest run. "
                "This populates with long-term quality candidates once market_coverage has run."
            )
        else:
            st.dataframe(_strategy_df(compounders), width="stretch", hide_index=True)
            st.caption(
                "Compounders: quality businesses held long-term. "
                "Exit triggers: −5% below 200 DMA, or 25% profit protection."
            )

    with tab_mom:
        if not momentum:
            st.info(
                "No momentum decisions in the latest run. "
                "This populates with event-driven or trend-following setups from the latest market scan."
            )
        else:
            st.dataframe(_strategy_df(momentum), width="stretch", hide_index=True)
            st.caption(
                "Momentum: event-driven or trend-following. "
                "Exit triggers: −3% below 50 DMA, or 12% profit protection."
            )

    with tab_sector:
        groupings = bundle.get("portfolio_view", {}).get("groupings") or {}
        by_sector = groupings.get("by_sector") or []
        if not by_sector:
            st.info("No sector grouping data available.")
        else:
            sec_rows = [
                {
                    "Sector":       g["name"],
                    "Count":        g["count"],
                    "Avg Conviction": _fmt_ratio_pct(g.get("avg_conviction_score")),
                    "Total Alloc":  _fmt_pct(g.get("total_suggested_allocation")),
                    "Tickers":      ", ".join(g.get("tickers") or []),
                }
                for g in sorted(by_sector, key=lambda x: x.get("total_suggested_allocation", 0), reverse=True)
            ]
            st.dataframe(_coerce_df(sec_rows), width="stretch", hide_index=True)
            portfolio_view = bundle.get("portfolio_view", {})
            if portfolio_view.get("warnings"):
                for w in portfolio_view["warnings"]:
                    st.warning(w)

    with tab_perf:
        perf = bundle.get("performance_view", {})
        if perf_summary is None:
            perf_summary = _load_performance_summary()

        if not perf.get("available") and not perf_summary:
            st.info("No resolved performance data yet. Trades need time to resolve before stats appear.")
        else:
            if perf.get("available"):
                dist = perf.get("return_distribution") or {}
                pc1, pc2, pc3, pc4 = st.columns(4)
                pc1.metric("Avg Return 5d",    _fmt_ratio_pct(dist.get("avg_return_5d")))
                pc2.metric("Median Return 5d", _fmt_ratio_pct(dist.get("median_return_5d")))
                pc3.metric("Strong Win Rate",  _fmt_ratio_pct(dist.get("strong_win_rate")))
                pc4.metric("Adverse Rate",     _fmt_ratio_pct(dist.get("adverse_rate")))

                calibration = perf.get("calibration_rows") or []
                if calibration:
                    cal_rows = [
                        {
                            "Confidence Bucket": r["bucket"],
                            "Hit Rate":          _fmt_ratio_pct(r["hit_rate"]),
                            "Avg Return 5d":     _fmt_ratio_pct(r["avg_return_5d"]),
                            "Sample":            r["attributable_count"],
                        }
                        for r in calibration
                    ]
                    st.dataframe(_coerce_df(cal_rows), width="stretch", hide_index=True)

            tracked = perf_summary.get("tracked_signals", 0)
            resolved = perf_summary.get("resolved_signals", 0)
            if tracked:
                st.caption(f"Signal tracking: {tracked} tracked, {resolved} resolved.")

            historically_strong = perf_summary.get("historically_strong_tickers") or []
            low_reliability = perf_summary.get("low_reliability_tickers") or []
            if historically_strong or low_reliability:
                sr_c1, sr_c2 = st.columns(2)
                with sr_c1:
                    if historically_strong:
                        st.markdown("**Historically strong:**")
                        for t in historically_strong[:5]:
                            st.markdown(f"- {_badge(t, 'good')}", unsafe_allow_html=True)
                with sr_c2:
                    if low_reliability:
                        st.markdown("**Low reliability:**")
                        for t in low_reliability[:5]:
                            st.markdown(f"- {_badge(t, 'bad')}", unsafe_allow_html=True)

        # Strategy recommendation — regime-based when no resolved data
        st.subheader("Strategy Recommendation")
        regime_label = bundle.get("overview", {}).get("market_regime") or ""
        regime_lower = regime_label.lower()
        resolved_count = int(_coerce_num((perf_summary or {}).get("resolved_signals"), 0))

        if resolved_count >= 5 and perf.get("available"):
            st.caption("Recommendation is performance-based (sufficient resolved data).")
        else:
            if "risk_on" in regime_lower:
                rec, rec_tone = "Favor Momentum", "good"
                rec_reason = "risk-on regime historically favors trend-following and event-driven setups"
            elif "risk_off" in regime_lower or "bear" in regime_lower:
                rec, rec_tone = "Favor Compounders", "warn"
                rec_reason = "defensive regime favors quality compounders with wider exit tolerance"
            elif "high_vol" in regime_lower or "volatile" in regime_lower:
                rec, rec_tone = "Reduce Exposure", "bad"
                rec_reason = "high volatility regime — reduce position sizes across both strategies"
            else:
                rec, rec_tone = "Balanced", "neutral"
                rec_reason = "neutral regime — balanced allocation between strategies is appropriate"
            st.markdown(_badge(rec, rec_tone), unsafe_allow_html=True)
            st.caption(rec_reason)
            if not resolved_count:
                st.caption(
                    f"Regime: {regime_label or 'unknown'} · Recommendation is regime-based only — "
                    "no resolved performance data available yet."
                )


def _render_exit_signals_tab(bundle: dict, mc: dict) -> None:
    st.subheader("Exit Signals")
    _render_mc_freshness(mc)
    _render_interpretation(
        "Signals to exit or reduce positions. Includes active SELL/TRIM decisions "
        "and low-conviction holdings that may warrant review."
    )

    decision_layer = mc.get("decision_layer") or {}
    actions = decision_layer.get("actions") or []
    exit_actions = [a for a in actions if a.get("action", "").upper() in {"SELL", "TRIM"}]

    snapshot_rows = bundle.get("portfolio_view", {}).get("rows") or []
    low_conviction = [
        r for r in snapshot_rows
        if str(r.get("conviction_band", "")).lower() in {"defer", "suppressed"}
    ]

    ec1, ec2 = st.columns(2)
    ec1.metric("Active Exit Decisions", len(exit_actions))
    ec2.metric("Low Conviction Holdings", len(low_conviction))

    if not exit_actions and not low_conviction:
        st.success("No active exit signals. All tracked positions are within acceptable conviction range.")
        return

    if exit_actions:
        st.subheader("Active Exit Decisions")
        for a in exit_actions:
            action_type = a.get("action", "SELL")
            tone = _action_tone(action_type)
            sym  = a.get("symbol", "?")
            strat = a.get("strategy_type") or "unknown"

            with st.expander(f"{action_type} — {sym}  [{strat}]", expanded=False):
                ea_c1, ea_c2 = st.columns(2)
                ea_c1.markdown(_badge(action_type, tone), unsafe_allow_html=True)
                score = a.get("score")
                if score is not None:
                    ea_c2.metric("Score", f"{score:.1f}")

                rationale = a.get("rationale") or []
                if rationale:
                    st.markdown("**Exit rationale:**")
                    for r in rationale:
                        is_trend = any(kw in r.lower() for kw in ["trend", "break", "weak", "below"])
                        t = "bad" if is_trend else "warn"
                        st.markdown(_badge("trigger", t) + f"  {r}", unsafe_allow_html=True)

                exit_plan = a.get("exit_plan") or {}
                triggers = exit_plan.get("triggers") or []
                if triggers:
                    st.caption("Trigger types: " + ", ".join(triggers))

                related = a.get("related_symbol")
                if related:
                    st.caption(f"Consider rotating into: {related}")

                alloc_pct = a.get("suggested_allocation_pct")
                alloc_amt = a.get("suggested_allocation_amount")
                if alloc_pct is not None or alloc_amt:
                    parts = []
                    if alloc_pct is not None:
                        parts.append(f"{alloc_pct * 100:.1f}%")
                    if alloc_amt:
                        parts.append(f"~${alloc_amt:,.0f}")
                    st.caption("Reduce to: " + " / ".join(parts))

    if low_conviction:
        st.subheader("Low Conviction Holdings (Watchlist)")
        st.caption(
            "These symbols scored below the actionable conviction threshold. "
            "Consider reviewing for removal or reduced weight."
        )
        lc_rows = [
            {
                "Symbol":     r.get("ticker", "?"),
                "Band":       r.get("conviction_band", "—"),
                "Conviction": _fmt_ratio_pct(r.get("conviction_score")),
                "Sector":     r.get("sector", "—"),
                "Alloc":      _fmt_pct(r.get("suggested_allocation")) if r.get("suggested_allocation") else "$0",
            }
            for r in sorted(low_conviction, key=lambda x: x.get("conviction_score", 0))
        ]
        st.dataframe(_coerce_df(lc_rows), width="stretch", hide_index=True)

    regime_commentary = bundle.get("portfolio_view", {}).get("regime_commentary") or ""
    if regime_commentary:
        st.caption(f"Regime context: {regime_commentary}")

    regime_label = bundle.get("overview", {}).get("market_regime") or ""
    if regime_label:
        regime_tone = "bad" if "risk_off" in regime_label.lower() else (
            "warn" if "high_vol" in regime_label.lower() else "good"
        )
        st.markdown(_badge(f"Regime: {regime_label}", regime_tone), unsafe_allow_html=True)


def _render_outcomes_tab(mc: dict, perf_summary: dict, outcomes_df: pd.DataFrame) -> None:
    st.subheader("Decision → Outcome")
    _render_interpretation(
        "Tracks every emitted signal from emission to resolution. "
        "Returns populate automatically once the evaluation window closes (1d / 3d / 7d)."
    )

    tracked  = int(_coerce_num(perf_summary.get("tracked_signals"), 0))
    resolved = int(_coerce_num(perf_summary.get("resolved_signals"), 0))

    m1, m2, m3 = st.columns(3)
    m1.metric("Signals Tracked", tracked)
    m2.metric("Resolved", resolved)
    m3.metric("Pending", max(0, tracked - resolved))

    if tracked == 0:
        st.info(
            "No signals tracked yet. Run the system to start populating the outcome log. "
            "Once signals are emitted they appear here and resolve after 1d / 3d / 7d."
        )
        return

    if outcomes_df.empty:
        st.info("Signal outcomes file not found or empty — check `outputs/performance/signal_outcomes.csv`.")
        return

    has_outcome = "outcome_return_3d" in outcomes_df.columns
    display_rows = []
    for _, row in outcomes_df.iterrows():
        ret_3d  = None
        status  = "Pending"
        if has_outcome:
            raw_ret = row.get("outcome_return_3d")
            if raw_ret is not None and str(raw_ret).strip() not in ("", "nan"):
                try:
                    ret_3d = float(raw_ret)
                    status = "Win" if row.get("outcome_success_3d") else "Loss"
                except (TypeError, ValueError):
                    pass

        signal_date = str(row.get("signal_time", "?"))[:10]
        ret_str = f"{ret_3d * 100:+.1f}%" if ret_3d is not None else "Pending"

        display_rows.append({
            "Ticker":       row.get("ticker", "?"),
            "Signal Date":  signal_date,
            "Intent":       str(row.get("prediction_intent", "?")).upper(),
            "Score":        _fmt_ratio_pct(row.get("signal_score")),
            "Confidence":   _fmt_ratio_pct(row.get("confidence_score")),
            "Band":         row.get("conviction_band") or "—",
            "3d Return":    ret_str,
            "Status":       status,
        })

    if display_rows:
        view_filter = st.radio(
            "Show",
            ["All signals", "Resolved only", "Pending only"],
            horizontal=True,
            key="outcomes_filter",
        )
        filtered_rows = display_rows
        if view_filter == "Resolved only":
            filtered_rows = [r for r in display_rows if r["Status"] != "Pending"]
        elif view_filter == "Pending only":
            filtered_rows = [r for r in display_rows if r["Status"] == "Pending"]

        if not filtered_rows:
            st.info("No records match the current filter.")
        else:
            st.dataframe(_coerce_df(filtered_rows), width="stretch", hide_index=True)

    if resolved == 0 and tracked > 0:
        st.info(
            f"{tracked} signals are being tracked — outcomes appear automatically once evaluation windows close. "
            "No manual action required."
        )

    strong = perf_summary.get("historically_strong_tickers") or []
    weak   = perf_summary.get("low_reliability_tickers") or []
    if strong or weak:
        sc1, sc2 = st.columns(2)
        with sc1:
            st.markdown("**Historically strong signals:**")
            for t in strong[:8]:
                st.markdown(f"- {_badge(t, 'good')}", unsafe_allow_html=True)
        with sc2:
            st.markdown("**Low reliability:**")
            for t in weak[:8]:
                st.markdown(f"- {_badge(t, 'bad')}", unsafe_allow_html=True)


def _render_execution_tab(mc: dict) -> None:
    st.subheader("Execution vs Recommendation")
    _render_interpretation(
        "Compares what the system recommended to what was actually executed. "
        "Requires trade event logging to be active."
    )

    trade_events_path = OUTPUTS_LATEST / "trade_events.json"
    trade_events = _load_json(trade_events_path)

    if not trade_events:
        st.info(
            "No execution log found. "
            "Trade events are logged automatically when the system executes recommendations. "
            "Expected at: `outputs/latest/trade_events.json`"
        )
        decision_layer = mc.get("decision_layer") or {}
        actions = decision_layer.get("actions") or []
        actionable = [
            a for a in actions
            if a.get("action", "").upper() in {"BUY", "SELL", "TRIM", "PROMOTE_TO_PORTFOLIO"}
        ]
        if actionable:
            st.subheader("Current Pending Recommendations")
            st.caption("Mark these as executed manually once confirmed.")
            rec_rows = [
                {
                    "Symbol":    a.get("symbol", "?"),
                    "Action":    a.get("action", "?"),
                    "Score":     f"{a.get('score', 0):.1f}" if a.get("score") is not None else "—",
                    "Confidence": f"{a.get('confidence', 0) * 100:.0f}%" if a.get("confidence") is not None else "—",
                    "Executed?": "—",
                }
                for a in actionable
            ]
            st.dataframe(_coerce_df(rec_rows), width="stretch", hide_index=True)
        return

    events = trade_events if isinstance(trade_events, list) else (trade_events.get("events") or [])
    if not events:
        st.info("Trade events file exists but contains no records.")
        return

    event_rows = [
        {
            "Symbol":    ev.get("symbol", "?"),
            "Action":    ev.get("action", "?"),
            "Executed":  "Yes" if ev.get("executed") else "No",
            "Timestamp": str(ev.get("timestamp", "?"))[:16],
            "Notes":     ev.get("notes") or "—",
        }
        for ev in events
    ]
    st.dataframe(_coerce_df(event_rows), width="stretch", hide_index=True)


# -- Attribution / Rotation panels -------------------------------------------

def _render_insights_tab(pa: dict, rot_events: list) -> None:
    """System Insights — read-only operator guidance synthesised from existing analytics."""
    st.subheader("System Insights")
    _render_interpretation(
        "Observe-only. Synthesises existing analytics artifacts into concise operator guidance. "
        "Does not modify any live decision behavior or backend analytics."
    )

    _STATUS_TONE: dict[str, str] = {
        "Healthy": "good",
        "Watch": "warn",
        "Investigate": "bad",
        "Insufficient Data": "neutral",
    }
    _TRUST_LABEL: dict[str, str] = {
        "high": "high confidence",
        "medium": "medium confidence",
        "low": "low confidence",
    }

    cards = _generate_insights(pa, rot_events)

    st.markdown("**At a Glance**")
    glance_cols = st.columns(len(cards))
    for col, card in zip(glance_cols, cards):
        tone = _STATUS_TONE.get(card.status, "neutral")
        with col:
            _render_operator_card(
                card.category,
                card.status,
                _TRUST_LABEL.get(card.trust, card.trust),
                badges=[_badge(card.status, tone)],
            )

    st.divider()
    st.markdown("**Guidance**")
    for card in cards:
        tone = _STATUS_TONE.get(card.status, "neutral")
        auto_expand = card.status in ("Watch", "Investigate")
        with st.expander(
            f"{card.category} — {card.title}",
            expanded=auto_expand,
        ):
            badge_row = (
                _badge(card.status, tone)
                + " "
                + _badge(_TRUST_LABEL.get(card.trust, card.trust), "neutral")
            )
            st.markdown(badge_row, unsafe_allow_html=True)
            st.markdown(card.guidance)
            if card.detail and card.detail != "—":
                st.caption(f"Supporting: {card.detail}")

    st.caption(
        "Observe-only — these interpretations do not change live behavior, "
        "thresholds, or analytics computations."
    )


def _render_attribution_tab(pa: dict) -> None:
    """Profit attribution overview split into three sub-tabs."""
    st.subheader("Profit Attribution")
    _render_interpretation(
        "Read-only. Shows what actually made money — "
        "Opportunity Attribution (scanner-level promotions) and "
        "Execution Attribution (system-recommended actions). "
        "Does not modify any live decision logic."
    )

    if not pa:
        st.info(
            "No attribution data yet. "
            "Expected at: `outputs/policy/profit_attribution.json` — "
            "this file is generated after coverage outcomes resolve."
        )
        return

    tab_ov, tab_ex, tab_bands = st.tabs(
        ["Opportunity Overview", "Execution", "Conf. Bands"]
    )

    # ------------------------------------------------------------------ #
    # Sub-tab 1: Opportunity overview
    # ------------------------------------------------------------------ #
    with tab_ov:
        st.markdown("**Opportunity Attribution** — scanner-promoted candidates")
        _render_interpretation(
            "Coverage attribution tracks every scanner promotion event "
            "through to its forward-return outcome."
        )
        m = pa.get("metrics") or {}
        total       = _coerce_num(m.get("total_entries"), 0)
        attributable = _coerce_num(m.get("attributable_entries"), 0)
        coverage     = _coerce_num(m.get("coverage_rate"), 0)
        win_rate     = m.get("win_rate")
        avg_gain     = m.get("avg_gain")
        avg_loss     = m.get("avg_loss")
        risk_reward  = m.get("risk_reward")
        expectancy   = m.get("expectancy")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Entries", int(total))
        c2.metric("Attributed", int(attributable))
        c3.metric("Coverage", _fmt_ratio_pct(coverage))
        c4.metric("Win Rate", _fmt_ratio_pct(win_rate) if win_rate is not None else "—")

        if win_rate is not None:
            e1, e2, e3, e4 = st.columns(4)
            e1.metric("Avg Gain", _fmt_ratio_pct(avg_gain) if avg_gain is not None else "—")
            e2.metric("Avg Loss", _fmt_ratio_pct(avg_loss) if avg_loss is not None else "—")
            e3.metric("Risk/Reward", f"{float(risk_reward):.2f}x" if risk_reward is not None else "—")
            e4.metric("Expectancy", _fmt_ratio_pct(expectancy) if expectancy is not None else "—")

        # Exit quality summary
        exit_summary = pa.get("exit_summary") or {}
        if any(v > 0 for v in exit_summary.values()):
            st.markdown("**Exit Quality Distribution**")
            exit_order = ["protected", "partial", "gave_back", "reversed", "no_gain", "unresolved"]
            exit_tone  = {"protected": "good", "partial": "warn", "gave_back": "warn",
                          "reversed": "bad", "no_gain": "bad", "unresolved": "neutral"}
            exit_html  = " ".join(
                _badge(f"{lbl}: {exit_summary[lbl]}", exit_tone.get(lbl, "neutral"))
                for lbl in exit_order if exit_summary.get(lbl, 0) > 0
            )
            st.markdown(exit_html, unsafe_allow_html=True)
            st.caption(
                "protected ≥70% of peak retained · partial 30–70% · "
                "gave_back <30% · reversed = gain turned to loss · no_gain = never rose"
            )

        # Best / worst trades
        best_trades  = pa.get("best_trades") or []
        worst_trades = pa.get("worst_trades") or []
        if best_trades or worst_trades:
            bw1, bw2 = st.columns(2)
            with bw1:
                if best_trades:
                    st.markdown("**Top Trades (T+5d)**")
                    st.dataframe(
                        _coerce_df([
                            {
                                "Symbol":   t.get("symbol", "?"),
                                "Strategy": t.get("strategy_type", "?"),
                                "Return 5d": _fmt_ratio_pct(t.get("return_5d")),
                                "Score":    f"{t.get('entry_score', 0):.0f}",
                                "Regime":   t.get("entry_regime", "?"),
                            }
                            for t in best_trades
                        ]),
                        width="stretch", hide_index=True,
                    )
            with bw2:
                if worst_trades:
                    st.markdown("**Worst Trades (T+5d)**")
                    st.dataframe(
                        _coerce_df([
                            {
                                "Symbol":   t.get("symbol", "?"),
                                "Strategy": t.get("strategy_type", "?"),
                                "Return 5d": _fmt_ratio_pct(t.get("return_5d")),
                                "Score":    f"{t.get('entry_score', 0):.0f}",
                            }
                            for t in worst_trades
                        ]),
                        width="stretch", hide_index=True,
                    )

        # Data quality notes
        dq_notes = pa.get("data_quality_notes") or []
        for note in dq_notes:
            st.caption(f"Data note: {note}")

        if int(total) == 0:
            st.info(
                "Attribution file exists but contains no entries yet — "
                "run the system after signals have resolved."
            )

    # ------------------------------------------------------------------ #
    # Sub-tab 2: Execution attribution
    # ------------------------------------------------------------------ #
    with tab_ex:
        st.markdown("**Execution Attribution** — system-recommended actions")
        _render_interpretation(
            "Advisory execution events from trade_events.jsonl. "
            "Answers: 'What actions the system recommended actually made money?' "
            "These are not broker fills — they are system-issued advisory signals."
        )
        ex = pa.get("execution")
        if not ex:
            st.info(
                "No execution attribution data. "
                "Execution tracking requires `trade_events.jsonl` to be present and "
                "coverage outcomes to have resolved."
            )
            return

        total_ev    = _coerce_num(ex.get("total_events"), 0)
        matched_ev  = _coerce_num(ex.get("matched_events"), 0)
        match_rate  = _coerce_num(ex.get("match_rate"), 0)

        f1, f2, f3 = st.columns(3)
        f1.metric("Events Logged",     int(total_ev))
        f2.metric("Matched to Outcome", int(matched_ev))
        f3.metric("Match Rate",         _fmt_ratio_pct(match_rate))

        by_action = ex.get("by_action") or []
        if by_action:
            action_rows = [
                {
                    "Action":           a.get("action", "?"),
                    "Events":           _coerce_num(a.get("total_events"), 0),
                    "Matched":          _coerce_num(a.get("matched_events"), 0),
                    "Win Rate":         _fmt_ratio_pct(a.get("win_rate")) if a.get("win_rate") is not None else "—",
                    "Avg Gain":         _fmt_ratio_pct(a.get("avg_gain")) if a.get("avg_gain") is not None else "—",
                    "Avg Loss":         _fmt_ratio_pct(a.get("avg_loss")) if a.get("avg_loss") is not None else "—",
                    "R/R":              f"{float(a['risk_reward']):.2f}x" if a.get("risk_reward") is not None else "—",
                    "Expectancy":       _fmt_ratio_pct(a.get("expectancy")) if a.get("expectancy") is not None else "—",
                    "Avg Exit Quality": _fmt_ratio_pct(a.get("avg_exit_quality")) if a.get("avg_exit_quality") is not None else "—",
                }
                for a in by_action
            ]
            st.markdown("**Performance by Action Type**")
            st.dataframe(_coerce_df(action_rows), width="stretch", hide_index=True)
            st.caption(
                "Win rate / gain / loss / R/R apply to BUY and PROMOTE events. "
                "Avg exit quality (latest return ÷ peak gain) is most meaningful for SELL and TRIM."
            )

        ex_dq = ex.get("data_quality_notes") or []
        for note in ex_dq:
            st.caption(f"Data note: {note}")

    # ------------------------------------------------------------------ #
    # Sub-tab 3: Confidence bands
    # ------------------------------------------------------------------ #
    with tab_bands:
        st.markdown("**Confidence Band Analysis**")
        _render_interpretation(
            "Observe-only. Does not change any thresholds or decision behavior. "
            "Tiers: low < 0.65 · medium 0.65–0.80 · high > 0.80. "
            "Events with no confidence value fall into low."
        )
        ex = pa.get("execution")
        if not ex:
            st.info(
                "No confidence band data. "
                "Requires execution attribution to be present with resolved outcomes."
            )
            return

        cal = ex.get("confidence_calibration") or {}
        cal_status = cal.get("status", "no_data")
        cal_tone = {
            "healthy": "good",
            "weak_separation": "warn",
            "insufficient_data": "neutral",
            "no_data": "neutral",
        }.get(cal_status, "neutral")
        st.markdown(
            _badge(f"Calibration: {cal_status}", cal_tone),
            unsafe_allow_html=True,
        )
        st.caption("observe_only — this analysis never modifies live confidence thresholds")

        band_order = ("low", "medium", "high")
        by_conf = {b.get("name", ""): b for b in (ex.get("by_confidence_band") or [])}

        band_rows = []
        for band in band_order:
            b = by_conf.get(band, {})
            total_b      = _coerce_num(b.get("total_entries"), 0)
            attributable = _coerce_num(b.get("attributable"), 0)
            small        = b.get("small_sample", False)
            win_rate_b   = b.get("win_rate")
            avg_gain_b   = b.get("avg_gain")
            avg_loss_b   = b.get("avg_loss")
            rr_b         = b.get("risk_reward")
            band_rows.append({
                "Band":      band,
                "Events":    int(total_b),
                "Matched":   int(attributable),
                "Win Rate":  _fmt_ratio_pct(win_rate_b) if win_rate_b is not None else "—",
                "Avg Gain":  _fmt_ratio_pct(avg_gain_b) if avg_gain_b is not None else "—",
                "Avg Loss":  _fmt_ratio_pct(avg_loss_b) if avg_loss_b is not None else "—",
                "R/R":       f"{float(rr_b):.2f}x" if rr_b is not None else "—",
                "Small?":    "⚠" if small else "",
            })
        if any(r["Events"] > 0 for r in band_rows):
            st.dataframe(_coerce_df(band_rows), width="stretch", hide_index=True)

        ss = cal.get("sample_summary") or {}
        low_m   = _coerce_num(ss.get("low_matched"), 0)
        med_m   = _coerce_num(ss.get("medium_matched"), 0)
        high_m  = _coerce_num(ss.get("high_matched"), 0)
        total_m = int(low_m + med_m + high_m)
        if total_m < 5:
            st.warning(
                f"Small-sample caution: only {total_m} matched execution events — "
                "calibration conclusions are not yet reliable."
            )
        elif total_m > 0:
            band_order_valid = cal.get("band_order_valid")
            if band_order_valid is True:
                st.markdown(
                    _badge("Band order valid: high ≥ medium ≥ low on win rate", "good"),
                    unsafe_allow_html=True,
                )
            elif band_order_valid is False:
                st.markdown(
                    _badge("Band order inverted — high confidence not outperforming low", "bad"),
                    unsafe_allow_html=True,
                )

            strongest = cal.get("strongest_band")
            weakest   = cal.get("weakest_band")
            if strongest:
                st.caption(f"Strongest band: {strongest}  |  Weakest: {weakest or '—'}")

        recommendation = cal.get("recommendation", "")
        rec_reason     = cal.get("recommendation_reason", "")
        if recommendation:
            st.markdown(f"**Recommendation (observe-only):** {recommendation}")
            if rec_reason:
                st.caption(f"Reason: {rec_reason}")


def _render_rotation_tab(rot_events: list) -> None:
    """Rotation quality panel — observe-only advisory."""
    st.subheader("Rotation Quality")
    _render_interpretation(
        "Observe-only. Tracks every rotation evaluation: when a challenger score was compared "
        "to an incumbent's. Helps identify whether small-margin rotations are noisy or whether "
        "momentum rotations add value. Does not modify rotation thresholds or exit behavior."
    )

    if not rot_events:
        st.info(
            "No rotation events yet. "
            "Expected at: `outputs/policy/rotation_events.jsonl` — "
            "populated automatically when exit evaluation runs with a challenger opportunity."
        )
        return

    total     = len(rot_events)
    triggered = sum(1 for e in rot_events if e.get("rotation_triggered"))
    not_trig  = total - triggered
    resolved  = sum(1 for e in rot_events if e.get("outcome_resolved"))
    degraded  = sum(1 for e in rot_events if e.get("degraded_mode"))

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Evaluations",  total)
    m2.metric("Triggered",          triggered)
    m3.metric("Not Triggered",      not_trig)
    m4.metric("Outcomes Resolved",  resolved)

    if degraded > 0:
        st.warning(f"{degraded} of {total} rotation evaluations occurred during degraded-data mode — treat those outcomes with caution.")

    # ----- By strategy -----
    strat_counts: dict = {}
    strat_triggered: dict = {}
    for e in rot_events:
        s = str(e.get("strategy_type") or "unknown")
        strat_counts[s]   = strat_counts.get(s, 0) + 1
        if e.get("rotation_triggered"):
            strat_triggered[s] = strat_triggered.get(s, 0) + 1

    if strat_counts:
        st.markdown("**By Strategy Type**")
        strat_rows = [
            {
                "Strategy":     s,
                "Evaluations":  strat_counts[s],
                "Triggered":    strat_triggered.get(s, 0),
                "Trigger Rate": _fmt_ratio_pct(strat_triggered.get(s, 0) / strat_counts[s]) if strat_counts[s] > 0 else "—",
            }
            for s in sorted(strat_counts)
        ]
        st.dataframe(_coerce_df(strat_rows), width="stretch", hide_index=True)

    # ----- Margin analysis -----
    margins = [e.get("actual_margin") for e in rot_events if e.get("actual_margin") is not None]
    req_margins = [e.get("required_margin") for e in rot_events if e.get("required_margin") is not None]
    if margins:
        avg_margin  = sum(margins) / len(margins)
        avg_req     = sum(req_margins) / len(req_margins) if req_margins else None

        small_margin_triggered = [
            e for e in rot_events
            if e.get("rotation_triggered") and e.get("actual_margin") is not None
            and e.get("required_margin") is not None
            and e["actual_margin"] < (e["required_margin"] * 1.25)
        ]

        ma1, ma2, ma3 = st.columns(3)
        ma1.metric("Avg Score Margin",    f"{avg_margin:+.1f}")
        ma2.metric("Avg Required Margin", f"{avg_req:+.1f}" if avg_req is not None else "—")
        ma3.metric("Small-Margin Triggers", len(small_margin_triggered),
                   help="Rotations triggered within 25% above the required margin — potentially noisy.")

        if len(small_margin_triggered) >= 3:
            st.warning(
                f"{len(small_margin_triggered)} small-margin rotation(s) were triggered. "
                "Consider reviewing the required_margin threshold once more outcomes resolve."
            )

    # ----- Breakout challenger analysis -----
    breakout_triggers = sum(
        1 for e in rot_events if e.get("rotation_triggered") and e.get("challenger_is_breakout")
    )
    if triggered > 0:
        st.markdown("**Challenger Type at Trigger**")
        bt1, bt2 = st.columns(2)
        bt1.metric("Breakout Challenger", breakout_triggers)
        bt2.metric("Non-Breakout",        triggered - breakout_triggers)

    # ----- Forward return summary (only when data is available) -----
    resolved_events = [
        e for e in rot_events
        if e.get("outcome_resolved") and e.get("forward_return_5d") is not None
    ]
    if resolved_events:
        trig_returns  = [e["forward_return_5d"] for e in resolved_events if e.get("rotation_triggered")]
        ntrig_returns = [e["forward_return_5d"] for e in resolved_events if not e.get("rotation_triggered")]

        st.markdown("**Forward Return (T+5d) — Observe-Only**")
        r1, r2 = st.columns(2)
        if trig_returns:
            avg_tr = sum(trig_returns) / len(trig_returns)
            wins   = sum(1 for r in trig_returns if r > 0)
            r1.metric(
                "Triggered — Avg Return",
                _fmt_ratio_pct(avg_tr),
                help=f"n={len(trig_returns)}, win rate {wins}/{len(trig_returns)}",
            )
        if ntrig_returns:
            avg_nt = sum(ntrig_returns) / len(ntrig_returns)
            wins_n = sum(1 for r in ntrig_returns if r > 0)
            r2.metric(
                "Not Triggered — Avg Return",
                _fmt_ratio_pct(avg_nt),
                help=f"n={len(ntrig_returns)}, win rate {wins_n}/{len(ntrig_returns)}",
            )

        if len(resolved_events) < 5:
            st.warning(
                f"Small-sample caution: only {len(resolved_events)} rotation events have resolved outcomes. "
                "These figures are observational only and should not drive threshold changes."
            )
    elif total > 0:
        st.caption(
            f"{total} rotation event(s) logged — forward outcomes not yet resolved. "
            "Check back after T+5d."
        )

    st.caption("Observe-only — no rotation thresholds or exit logic is modified by this panel.")


# -- v2 helpers --------------------------------------------------------------


def _get_llm_status(cfg: dict) -> dict:
    """
    Check OpenAI reachability (the primary LLM provider).
    Returns dict: running, base_url, model, model_available, error,
    timed_out, timeout_seconds, latency_ms, provider.
    """
    result = {
        "running": False, "base_url": "", "model": "", "provider": "openai",
        "model_available": False, "error": "",
        "timed_out": False, "timeout_seconds": 20, "latency_ms": None,
    }
    try:
        timeout = max(
            5,
            int(
                os.environ.get(
                    "LLM_HEALTH_TIMEOUT",
                    cfg.get("theme_engine", {}).get("llm_health_timeout_seconds", 20),
                )
            ),
        )
        check = validate_openai_connection(timeout=timeout)
        result["timeout_seconds"] = timeout
        result["provider"] = check.get("provider", "openai")
        result["base_url"] = check.get("base_url", "")
        result["model"] = check.get("model", "")
        result["latency_ms"] = check.get("latency_ms")
        result["model_available"] = bool(check.get("ok"))
        message = str(check.get("message", "") or "")
        message_lower = message.lower()
        result["timed_out"] = "timed out" in message_lower
        result["running"] = bool(check.get("ok")) or result["timed_out"]
        if not check.get("ok"):
            result["error"] = message
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _load_prompts() -> list:
    """Load saved prompts from data/prompts.json."""
    data = _load_json(PROMPTS_PATH)
    return data.get("prompts", []) if isinstance(data, dict) else []


def _save_prompts(prompts: list):
    try:
        PROMPTS_PATH.write_text(
            json.dumps({"prompts": prompts}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return None
    except Exception as e:
        return str(e)


def _load_wl_tags() -> dict:
    """Load watchlist tags/metadata from data/watchlist_tags.json."""
    data = _load_json(WL_TAGS_PATH)
    return data if isinstance(data, dict) else {}


def _save_wl_tags(tags: dict):
    try:
        WL_TAGS_PATH.write_text(
            json.dumps(tags, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return None
    except Exception as e:
        return str(e)


def _zip_latest_outputs() -> bytes:
    """Create an in-memory zip of outputs/latest/."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in OUTPUTS_LATEST.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(OUTPUTS_LATEST))
    buf.seek(0)
    return buf.read()


# ============================================================================
# PAGE: DECISION CENTER
# ============================================================================

def page_decision_center() -> None:
    _operator_dashboard_css()
    bundle = load_operator_dashboard_data(ROOT)

    title_col, action_col = st.columns([5, 1])
    with title_col:
        st.title("Decision Center")
        st.caption(
            "Observe-only advisory decision plan. "
            "All decisions are derived from `outputs/latest/decision_plan.json`. "
            "No recomputation occurs here and no trades are executed."
        )
    with action_col:
        if st.button("Refresh", width="stretch", key="dc_refresh"):
            st.rerun()

    _render_decision_brief_summary(bundle)
    st.divider()
    _render_decision_triage_section(bundle)
    st.divider()
    _render_ai_validation_section(bundle)
    st.divider()
    _render_system_confidence_section(bundle)
    st.divider()
    _render_decision_performance_section(bundle)
    st.divider()
    _render_decision_performance_attribution(bundle)


# ============================================================================
# SIDEBAR
# ============================================================================

cfg      = _load_config()
last_ok  = _load_json(DATA_DIR / "last_success.json")
drawdown = _load_json(DATA_DIR / "drawdown_state.json")

st.sidebar.title("StockBot")
st.sidebar.caption("Operator Dashboard")

PAGES = [
    "Dashboard", "Decision Center", "Automatic Promotion",
    "Run Controls", "Outputs", "Watchlist", "Run History",
    "Production Health", "API Health",
    "Config Editor", "Prompts", "Logs", "Diagnostics",
]

page = st.sidebar.radio("nav", PAGES, label_visibility="collapsed", key="nav_page")

inv_name  = cfg.get("investor", {}).get("name", "--")
total_val = last_ok.get("total_value", drawdown.get("current_value", 0.0))
regime    = last_ok.get("drawdown_regime", "--")
ath       = drawdown.get("all_time_high", 0.0)
dd_pct    = ((ath - total_val) / ath * 100) if ath > 0 else 0.0

st.sidebar.markdown(f"**Investor:** {inv_name}")

if last_ok:
    ts   = last_ok.get("timestamp", "")[:16]
    mode = last_ok.get("run_mode", "?")
    st.sidebar.markdown(
        f"**Value:** `${total_val:,.2f}`  \n"
        f"**ATH:** `${ath:,.2f}` (DD {dd_pct:.1f}%)  \n"
        f"**Last run:** `{mode}` @ {ts}  \n"
        f"**Regime:** `{regime}`"
    )
else:
    st.sidebar.warning("No successful run yet")

st.sidebar.divider()
st.sidebar.caption(f"`{ROOT}`")


# ============================================================================
# PAGE: DASHBOARD
# ============================================================================

def page_dashboard() -> None:
    _operator_dashboard_css()
    bundle       = load_operator_dashboard_data(ROOT)
    mc           = _load_market_opportunities()
    perf_summary = _load_performance_summary()
    outcomes_df  = _load_signal_outcomes_df()
    pa           = _load_profit_attribution()
    rot_events   = _load_rotation_events()

    title_col, action_col = st.columns([5, 1])
    with title_col:
        st.title("Operator Dashboard")
        st.caption(
            "Artifact-driven visibility into the latest advisory outputs. "
            "Missing files degrade gracefully and the dashboard does not alter live investing behavior."
        )
    with action_col:
        if st.button("Refresh", width="stretch"):
            st.rerun()

    # Operator Cockpit summary — card-based at-a-glance landing.  Additive,
    # read-only, beginner-friendly.  Uses the reusable helpers added in the
    # gui_operator_cockpit_redesign track.  Existing dashboard sections
    # below remain unchanged for power users.
    _render_cockpit_summary_grid(bundle)
    st.divider()

    _render_system_summary()
    _render_daily_memo_section()
    st.divider()
    _render_system_confidence_indicator(perf_summary)
    _render_action_strip(mc, bundle)
    _render_portfolio_health_row(bundle, mc)
    st.divider()

    mode = st.radio(
        "Dashboard mode",
        ["Overview", "Advanced"],
        horizontal=True,
        key="operator_dashboard_mode",
    )

    if mode == "Overview":
        _render_overview_mode(bundle, mc)
        return

    (
        tab_insights,
        tab_decisions, tab_opps, tab_pvm, tab_strat_break, tab_exits,
        tab_outcomes, tab_execution,
        tab_status, tab_memo, tab_triage, tab_portfolio, tab_strategy,
        tab_health, tab_performance, tab_regime, tab_enrichment, tab_quality,
        tab_attribution, tab_rotation,
        tab_weekly,
        tab_data_quality, tab_ai_budget, tab_calibration, tab_discovery,
    ) = st.tabs(
        [
            "Insights",
            "Decision Center",
            "Opportunities",
            "Portfolio vs Market",
            "Strategy Breakdown",
            "Exit Signals",
            "Outcomes",
            "Execution",
            "Run Status",
            "Memo Review",
            "Signal Triage",
            "Portfolio",
            "Strategy",
            "Health",
            "Performance",
            "Regime",
            "Signal Enrichment",
            "Rec. Quality",
            "Attribution",
            "Rotation",
            "Weekly Review",
            "Data Quality",
            "AI Budget",
            "Calibration",
            "Discovery",
        ]
    )

    with tab_insights:
        _render_insights_tab(pa, rot_events)
    with tab_decisions:
        _render_decision_center_tab(bundle, mc)
    with tab_opps:
        _render_opportunities_tab(bundle, mc)
    with tab_pvm:
        _render_portfolio_vs_market_tab(bundle, mc)
    with tab_strat_break:
        _render_strategy_breakdown_tab(bundle, mc, perf_summary)
    with tab_exits:
        _render_exit_signals_tab(bundle, mc)
    with tab_outcomes:
        _render_outcomes_tab(mc, perf_summary, outcomes_df)
    with tab_execution:
        _render_execution_tab(mc)
    with tab_status:
        _render_run_status_tab(bundle)
    with tab_memo:
        _render_memo_tab(bundle)
    with tab_triage:
        _render_signal_triage_tab(bundle)
    with tab_portfolio:
        _render_portfolio_tab(bundle)
    with tab_strategy:
        _render_strategy_tab(bundle)
    with tab_health:
        _render_health_tab(bundle)
    with tab_performance:
        _render_performance_tab(bundle)
    with tab_regime:
        _render_regime_analytics_tab(bundle)
    with tab_enrichment:
        _render_signal_enrichment_tab(perf_summary)
    with tab_quality:
        _render_recommendation_quality_tab(bundle)
    with tab_attribution:
        _render_attribution_tab(pa)
    with tab_rotation:
        _render_rotation_tab(rot_events)
    with tab_weekly:
        _render_weekly_review_tab(bundle)
    with tab_data_quality:
        _render_data_quality_tab(bundle)
    with tab_ai_budget:
        _render_ai_budget_tab(bundle)
    with tab_calibration:
        _render_calibration_tab(bundle)
    with tab_discovery:
        _render_discovery_sandbox_tab(bundle)


# ============================================================================
# PAGE: RUN CONTROLS
# ============================================================================

def page_run_controls() -> None:
    st.title("Run Controls")
    st.caption(
        "Runs are **synchronous** -- output appears when the process exits. "
        "Dry run is on by default so nothing is written until you're ready."
    )

    with st.expander("Run Options", expanded=True):
        o1, o2, o3 = st.columns(3)
        dry_run    = o1.checkbox("Dry run (no files / no email)", value=True,
                                 help="Passes --dry-run. Always safe.")
        debug_mode = o2.checkbox("Debug logging", value=False,
                                 help="Passes --debug. Very verbose output.")
        skip_email = o3.checkbox("Skip email", value=True,
                                 help="Passes --skip-email even if email.enabled=true in config.")

    def _main_cmd(mode: str) -> list:
        cmd = [PYTHON, "main.py", "--run-mode", mode]
        if dry_run:    cmd.append("--dry-run")
        if debug_mode: cmd.append("--debug")
        if skip_email: cmd.append("--skip-email")
        return cmd

    st.subheader("Main Portfolio Analysis")
    m1, m2, m3 = st.columns(3)
    run_daily   = m1.button("Run Daily",   width="stretch", type="primary",
                            help="Alert-only digest; uses cached prices if < 24 h old.")
    run_weekly  = m2.button("Run Weekly",  width="stretch",
                            help="Full digest + S&P 500 watchlist refresh (~3 FMP calls).")
    run_monthly = m3.button("Run Monthly", width="stretch",
                            help="Capital deployment memo + full S&P 500 scan.")

    if run_daily:
        with st.spinner("Running daily analysis..."):
            rc, out = _run_command(_main_cmd("daily"))
        _store_run(rc, out, "Daily analysis")

    if run_weekly:
        with st.spinner("Running weekly analysis..."):
            rc, out = _run_command(_main_cmd("weekly"))
        _store_run(rc, out, "Weekly analysis")

    if run_monthly:
        with st.spinner("Running monthly analysis..."):
            rc, out = _run_command(_main_cmd("monthly"))
        _store_run(rc, out, "Monthly analysis")

    st.divider()

    st.subheader("Sub-systems")
    s1, s2, s3 = st.columns(3)

    with s1:
        st.markdown("**AI Agent** (`python -m agent`)")
        agent_mode    = st.selectbox("Mode", ["daily", "weekly", "monthly"], key="ag_mode")
        agent_offline = st.checkbox("Force offline (no LLM)", key="ag_offline",
                                    help="--no-network: templated memo, no LLM provider needed.")
        if st.button("Run AI Agent", width="stretch", key="btn_agent"):
            cmd = [PYTHON, "-m", "agent", "--mode", agent_mode]
            if agent_offline:
                cmd.append("--no-network")
            with st.spinner(f"Running agent ({agent_mode})..."):
                rc, out = _run_command(cmd, timeout=180)
            _store_run(rc, out, f"AI Agent ({agent_mode})")

    with s2:
        st.markdown("**Watchlist Scanner** (`python -m watchlist_scanner`)")
        wl_dry = st.checkbox("Dry run", value=True, key="wl_dry",
                             help="Uses cached data only -- no live API calls.")
        if st.button("Run Watchlist Scanner", width="stretch", key="btn_wl"):
            cmd = [PYTHON, "-m", "watchlist_scanner"]
            if wl_dry:
                cmd.append("--dry-run")
            with st.spinner("Running watchlist scanner..."):
                rc, out = _run_command(cmd, timeout=120)
            _store_run(rc, out, "Watchlist Scanner")

    with s3:
        st.markdown("**Theme Engine** (`python -m theme_engine`)")
        theme_mode = st.selectbox("Mode", ["daily", "weekly", "monthly"], key="th_mode")
        if st.button("Run Theme Engine", width="stretch", key="btn_theme"):
            cmd = [PYTHON, "-m", "theme_engine", "--mode", theme_mode]
            with st.spinner(f"Running theme engine ({theme_mode})..."):
                rc, out = _run_command(cmd, timeout=120)
            _store_run(rc, out, f"Theme Engine ({theme_mode})")

    st.divider()

    st.subheader("Run Console")
    if "run_out" not in st.session_state:
        st.info("No run output yet -- press a button above to launch a workflow.")
        return

    rc    = st.session_state["run_rc"]
    label = st.session_state["run_label"]
    out   = st.session_state["run_out"]

    if rc == 0:
        st.success(f"Exit 0 -- **{label}** completed successfully.")
    elif rc == -1:
        st.warning(f"**{label}** -- timeout or startup error.")
    else:
        st.error(f"Exit {rc} -- **{label}** failed.")

    st.text_area("Output", out, height=450, key="console_area",
                 help="Full stdout + stderr from the process.")

    dl, clr = st.columns(2)
    dl.download_button(
        "Download as .txt",
        out.encode("utf-8"),
        file_name=f"run_{datetime.now():%Y%m%d_%H%M%S}.txt",
    )
    if clr.button("Clear console"):
        for k in ("run_out", "run_rc", "run_label"):
            st.session_state.pop(k, None)
        st.rerun()


# ============================================================================
# PAGE: OUTPUTS VIEWER
# ============================================================================

def page_outputs() -> None:
    st.title("Outputs Viewer")
    scopes = ["Latest", "Portfolio", "Policy", "Regime", "Reports", "History"]
    scope = st.radio(
        "Artifact scope",
        scopes,
        horizontal=True,
        key="outputs_scope",
    )

    if scope == "Latest":
        _render_output_scope_browser("Latest", OUTPUTS_LATEST)
        return

    if scope == "Portfolio":
        _render_output_scope_browser("Portfolio", ROOT / "outputs" / "portfolio")
        return

    if scope == "Policy":
        _render_output_scope_browser("Policy", ROOT / "outputs" / "policy")
        return

    if scope == "Regime":
        _render_output_scope_browser("Regime", ROOT / "outputs" / "regime")
        return

    if scope == "Reports":
        _render_output_scope_browser("Reports", ROOT / "outputs" / "reports")
        return

    if not OUTPUTS_HISTORY.exists():
        st.info("No history directory found yet.")
        return

    date_dirs = sorted(
        [d for d in OUTPUTS_HISTORY.iterdir() if d.is_dir()], reverse=True
    )
    if not date_dirs:
        st.info("No history snapshots yet.")
        return

    sel_date = st.selectbox("Date", [d.name for d in date_dirs], key="outputs_history_date")
    if sel_date:
        hist_files = sorted(
            [f for f in (OUTPUTS_HISTORY / sel_date).iterdir() if f.is_file()]
        )
        if not hist_files:
            st.info("No history files exist for that date.")
            return
        hist_map = {f.name: f for f in hist_files}
        desired = st.session_state.get("outputs_selected_file")
        hist_options = list(hist_map.keys())
        hist_index = hist_options.index(desired) if desired in hist_options else 0
        sel_hist = st.selectbox("File", hist_options, index=hist_index, key="hist_sel")
        st.session_state["outputs_selected_file"] = sel_hist
        hp = hist_map[sel_hist]
        st.caption(f"`{hp.relative_to(ROOT)}` -- {hp.stat().st_size:,} bytes")
        _render_file(hp)


# ============================================================================
# PAGE: WATCHLIST MANAGER
# ============================================================================

def page_watchlist_manager() -> None:
    st.title("Watchlist Manager")
    cfg_raw  = _load_config()
    wl_cfg   = cfg_raw.get("watchlist_scanner", {})
    symbols  = list(wl_cfg.get("watchlist", []))
    tags_db  = _load_wl_tags()  # {symbol: {tags: [...], enabled: bool, note: str}}

    PREDEFINED_TAGS = [
        "AI", "Semis", "Crypto", "Energy", "Financials",
        "Healthcare", "Core ETF", "Leverage", "Growth", "Value",
    ]

    tab_view, tab_edit, tab_import = st.tabs(["View & Tags", "Add / Remove", "Import / Export"])

    # -- View & Tags ----------------------------------------------------------
    with tab_view:
        if not symbols:
            st.info("No symbols in watchlist. Add some in the 'Add / Remove' tab.")
        else:
            rows = []
            for sym in symbols:
                meta = tags_db.get(sym, {})
                rows.append({
                    "Symbol":  sym,
                    "Enabled": meta.get("enabled", True),
                    "Tags":    ", ".join(meta.get("tags", [])),
                    "Note":    meta.get("note", ""),
                })
            df = pd.DataFrame(rows)
            st.dataframe(df, width="stretch", hide_index=True)
            enabled_count = sum(1 for r in rows if r["Enabled"])
            st.caption(f"{len(symbols)} symbols -- {enabled_count} enabled")

            st.subheader("Edit Symbol Metadata")
            sel_sym = st.selectbox("Symbol to edit", symbols, key="wl_tag_sym")
            if sel_sym:
                meta        = tags_db.get(sel_sym, {})
                cur_tags    = meta.get("tags", [])
                cur_enabled = meta.get("enabled", True)
                cur_note    = meta.get("note", "")

                new_enabled = st.checkbox("Enabled", value=cur_enabled, key="wl_enabled")
                new_tags    = st.multiselect(
                    "Tags", PREDEFINED_TAGS,
                    default=[t for t in cur_tags if t in PREDEFINED_TAGS],
                    key="wl_tag_sel",
                )
                custom_tags = st.text_input(
                    "Custom tags (comma-separated)",
                    value=", ".join(t for t in cur_tags if t not in PREDEFINED_TAGS),
                    key="wl_custom_tags",
                )
                new_note = st.text_input("Note", value=cur_note, key="wl_note")

                if st.button("Save metadata", key="btn_save_meta"):
                    all_tags = new_tags + [
                        t.strip() for t in custom_tags.split(",") if t.strip()
                    ]
                    tags_db[sel_sym] = {
                        "enabled": new_enabled,
                        "tags":    all_tags,
                        "note":    new_note,
                    }
                    err = _save_wl_tags(tags_db)
                    if not err:
                        st.success(f"Saved metadata for {sel_sym}.")
                    else:
                        st.error(err)

            # Group-by-tag view
            if tags_db:
                st.subheader("View by Tag")
                all_tags_flat = sorted(set(
                    t for v in tags_db.values() for t in v.get("tags", [])
                ))
                if all_tags_flat:
                    sel_tag = st.selectbox(
                        "Filter by tag", ["(all)"] + all_tags_flat, key="wl_filter_tag"
                    )
                    if sel_tag != "(all)":
                        tagged = [
                            s for s, v in tags_db.items()
                            if sel_tag in v.get("tags", [])
                        ]
                        st.markdown(
                            f"**{len(tagged)} symbols tagged `{sel_tag}`:** " +
                            " | ".join(f"`{s}`" for s in tagged)
                        )

    # -- Add / Remove ---------------------------------------------------------
    with tab_edit:
        st.subheader("Current Symbols")
        st.code(", ".join(symbols) if symbols else "(empty)", language=None)

        st.subheader("Add Symbols")
        add_txt = st.text_input("Symbols to add (comma-separated, e.g. AAPL, NVDA)")
        if st.button("Add", key="btn_wl_add"):
            new_syms = [s.strip().upper() for s in add_txt.split(",") if s.strip()]
            added = [s for s in new_syms if s not in symbols]
            if added:
                symbols = symbols + added
                cfg_raw.setdefault("watchlist_scanner", {})["watchlist"] = symbols
                err = _save_config(cfg_raw)
                if not err:
                    st.success(f"Added: {', '.join(added)}")
                else:
                    st.error(err)
            else:
                st.info("All symbols already in watchlist (or input empty).")

        st.subheader("Remove Symbols")
        if symbols:
            to_remove = st.multiselect("Select symbols to remove", symbols, key="wl_remove")
            if st.button("Remove selected", key="btn_wl_remove"):
                new_list = [s for s in symbols if s not in to_remove]
                cfg_raw.setdefault("watchlist_scanner", {})["watchlist"] = new_list
                err = _save_config(cfg_raw)
                if not err:
                    for s in to_remove:
                        tags_db.pop(s, None)
                    _save_wl_tags(tags_db)
                    st.success(f"Removed: {', '.join(to_remove)}")
                else:
                    st.error(err)

        st.subheader("Bulk Replace (overwrite entire list)")
        bulk_txt = st.text_area(
            "Comma-separated symbols", value=", ".join(symbols), height=80, key="wl_bulk"
        )
        if st.button("Save bulk list", key="btn_wl_bulk"):
            new_syms = [s.strip().upper() for s in bulk_txt.split(",") if s.strip()]
            cfg_raw.setdefault("watchlist_scanner", {})["watchlist"] = new_syms
            err = _save_config(cfg_raw)
            if not err:
                st.success(f"Saved {len(new_syms)} symbols.")
            else:
                st.error(err)

    # -- Import / Export ------------------------------------------------------
    with tab_import:
        st.subheader("Export")
        export_data = {
            "watchlist": symbols,
            "tags":      tags_db,
            "exported":  datetime.now().isoformat(),
        }
        st.download_button(
            "Export watchlist + tags as JSON",
            json.dumps(export_data, indent=2).encode("utf-8"),
            file_name=f"watchlist_export_{date.today()}.json",
            mime="application/json",
        )

        st.subheader("Import")
        uploaded = st.file_uploader("Upload watchlist JSON", type=["json"], key="wl_upload")
        if uploaded:
            try:
                imported  = json.loads(uploaded.read())
                imp_syms  = imported.get("watchlist", [])
                imp_tags  = imported.get("tags", {})
                preview   = ", ".join(imp_syms[:10])
                more      = "..." if len(imp_syms) > 10 else ""
                st.write(f"Preview: {len(imp_syms)} symbols -- {preview}{more}")
                if st.button("Apply import", key="btn_wl_import"):
                    cfg_raw.setdefault("watchlist_scanner", {})["watchlist"] = imp_syms
                    err = _save_config(cfg_raw)
                    if not err:
                        _save_wl_tags(imp_tags)
                        st.success(f"Imported {len(imp_syms)} symbols.")
                    else:
                        st.error(err)
            except Exception as exc:
                st.error(f"Invalid JSON: {exc}")


# ============================================================================
# PAGE: RUN HISTORY
# ============================================================================

def page_run_history() -> None:
    st.title("Run History")

    # SQLite run_history
    st.subheader("Run History (database)")
    db_rows = _query_db(
        "SELECT run_id, mode, status, started_at, completed_at "
        "FROM run_history ORDER BY started_at DESC LIMIT 100"
    )

    if db_rows:
        df = pd.DataFrame(db_rows)

        fc1, fc2, fc3 = st.columns(3)
        modes    = ["(all)"] + sorted(df["mode"].dropna().unique().tolist())
        statuses = ["(all)"] + sorted(df["status"].dropna().unique().tolist())
        f_mode   = fc1.selectbox("Mode", modes, key="rh_mode")
        f_status = fc2.selectbox("Status", statuses, key="rh_status")
        f_limit  = fc3.number_input("Show last N rows", 5, 100, 30, 5, key="rh_limit")

        mask = pd.Series([True] * len(df))
        if f_mode != "(all)":
            mask &= df["mode"] == f_mode
        if f_status != "(all)":
            mask &= df["status"] == f_status

        display = df[mask].head(int(f_limit))
        st.dataframe(display, width="stretch", hide_index=True)
        st.caption(f"{len(display)} rows shown ({len(df)} total in DB)")
    else:
        st.info("No run history in database yet. Run the portfolio analysis first.")

    st.divider()

    # Output snapshots (history directory)
    st.subheader("Output Snapshots")

    if not OUTPUTS_HISTORY.exists():
        st.info("`outputs/history/` does not exist yet. Created after the first real run.")
        return

    date_dirs = sorted(
        [d for d in OUTPUTS_HISTORY.iterdir() if d.is_dir()], reverse=True
    )
    if not date_dirs:
        st.info("No history snapshots yet.")
        return

    hist_summary = []
    for d in date_dirs:
        files = list(d.iterdir())
        kb = sum(f.stat().st_size for f in files if f.is_file()) / 1024
        hist_summary.append({
            "Date":     d.name,
            "Files":    len(files),
            "Size KB":  f"{kb:.1f}",
            "Age":      _file_age(d),
        })
    st.dataframe(pd.DataFrame(hist_summary), width="stretch", hide_index=True)

    sel_date = st.selectbox("Inspect snapshot", [d.name for d in date_dirs], key="rh_date")
    if sel_date:
        snap_dir   = OUTPUTS_HISTORY / sel_date
        snap_files = sorted([f for f in snap_dir.iterdir() if f.is_file()])
        if snap_files:
            snap_map = {f.name: f for f in snap_files}
            sel_snap = st.selectbox("File", list(snap_map.keys()), key="rh_snap")
            if sel_snap:
                fp = snap_map[sel_snap]
                st.caption(f"`{fp.name}` -- {fp.stat().st_size:,} bytes")
                _render_file(fp)
                with open(fp, "rb") as fh:
                    st.download_button(
                        f"Download {sel_snap}", fh.read(), file_name=sel_snap, key="rh_dl"
                    )

    st.divider()

    # Portfolio peak history (DB)
    st.subheader("Portfolio Peaks (database)")
    peaks = _query_db(
        "SELECT * FROM portfolio_peaks ORDER BY recorded_at DESC LIMIT 20"
    )
    if peaks:
        st.dataframe(pd.DataFrame(peaks), width="stretch", hide_index=True)
    else:
        st.info("No portfolio_peaks records yet.")


# ============================================================================
# PAGE: API HEALTH  (feature 1 + 2)
# ============================================================================

def page_api_health() -> None:
    st.title("API & Model Health")
    cfg_raw = _load_config()

    # -- LLM reachability (OpenAI primary, Anthropic fallback) ----------------
    st.subheader("LLM Provider (OpenAI primary)")
    llm = _get_llm_status(cfg_raw)

    o1, o2 = st.columns(2)
    o1.markdown(f"**Provider:** `{llm['provider']}`")
    o2.markdown(f"**Base URL:** `{llm['base_url'] or '(unset)'}`")
    st.markdown(f"**Configured model:** `{llm['model'] or '(unset)'}`")

    if llm["model_available"]:
        latency = llm.get("latency_ms")
        latency_note = f" ({latency}ms)" if latency is not None else ""
        st.success(f"OpenAI: **reachable**{latency_note}")
        st.success(f"Model `{llm['model']}`: **responding**")
    elif llm.get("timed_out"):
        st.warning(
            f"OpenAI health check timed out after {llm.get('timeout_seconds', 20)}s.  \n"
            f"Model `{llm['model']}` may be responding slowly right now.  \n"
            "Fix: raise `LLM_HEALTH_TIMEOUT`, or retry."
        )
    else:
        st.error(
            f"OpenAI: **not reachable**  \n"
            f"Error: `{llm['error']}`  \n"
            "Fix: set `OPENAI_API_KEY` + `OPENAI_MODEL` in your .env, and verify "
            "`OPENAI_BASE_URL` points to an OpenAI-compatible /v1 endpoint."
        )
        st.info(
            "OpenAI is the primary LLM provider (Anthropic is the fallback). "
            "It is only required for the Theme Engine and AI Agent (daily/weekly modes). "
            "Portfolio analysis runs without it."
        )

    st.divider()

    # -- Other API keys -------------------------------------------------------
    st.subheader("Other API Keys")
    for key, info in _env_status().items():
        row1, row2 = st.columns([4, 1])
        row1.markdown(f"**`{key}`** -- {info['desc']}")
        if info["set"]:
            row2.success("Set")
        else:
            row2.error("Missing")

    st.divider()

    # -- Network checks -------------------------------------------------------
    st.subheader("Network Connectivity")
    nc1, nc2 = st.columns(2)

    if nc1.button("Ping api.anthropic.com", key="btn_claude_dns"):
        try:
            with urllib.request.urlopen("https://api.anthropic.com", timeout=5) as r:
                st.success(f"api.anthropic.com reachable (HTTP {r.status})")
        except Exception as exc:
            if "401" in str(exc) or "403" in str(exc):
                st.success("api.anthropic.com reachable (auth required -- expected)")
            else:
                st.error(f"api.anthropic.com unreachable: {exc}")


# ============================================================================
# PAGE: CONFIG EDITOR
# ============================================================================

def page_config_editor() -> None:
    st.title("Configuration Editor")
    cfg_raw = _load_config()

    tab_port, tab_hold, tab_feat, tab_env = st.tabs(
        ["Portfolio & Rules", "Holdings", "Features & Services", "Secrets (.env)"]
    )

    # -- Portfolio & Rules ----------------------------------------------------
    with tab_port:
        inv = cfg_raw.get("investor", {})
        st.subheader("Investor Profile (read-only)")
        r1, r2 = st.columns(2)
        r1.text_input("Name",   value=str(inv.get("name", "")),   disabled=True)
        r1.number_input("Age",  value=int(inv.get("age", 0)),     disabled=True)
        r1.text_input("Risk Tolerance", value=str(inv.get("risk_tolerance", "")), disabled=True)
        r2.number_input("Annual Income ($)",    value=float(inv.get("annual_income", 0)),    disabled=True)
        r2.number_input("Monthly Expenses ($)", value=float(inv.get("monthly_expenses", 0)), disabled=True)
        r2.number_input("Horizon (years)",      value=int(inv.get("investment_horizon_years", 0)), disabled=True)
        st.caption("Edit config.json directly to change profile fields.")

        st.divider()
        st.subheader("Editable Settings")
        with st.form("form_portfolio"):
            p  = cfg_raw.get("portfolio", {})
            rr = cfg_raw.get("rebalance_rules", {})
            gm = cfg_raw.get("growth_mode", {})

            f1, f2 = st.columns(2)
            new_cash    = f1.number_input("Cash Available ($)",       value=float(p.get("cash_available", 0)),       step=50.0,  format="%.2f")
            new_contrib = f2.number_input("Monthly Contribution ($)", value=float(p.get("monthly_contribution", 0)), step=100.0, format="%.2f")

            gm_opts  = ["accumulation_aggressive", "accumulation_moderate", "conservation"]
            cur_gm   = gm.get("mode", "accumulation_aggressive")
            new_mode = st.selectbox("Growth Mode", gm_opts,
                                    index=gm_opts.index(cur_gm) if cur_gm in gm_opts else 0)

            s1, s2, s3 = st.columns(3)
            new_band = s1.slider("Band Threshold",    0.01, 0.30, float(rr.get("band_threshold",   0.12)), 0.01)
            new_conc = s2.slider("Concentration Cap", 0.20, 0.60, float(gm.get("concentration_cap", 0.40)), 0.01)
            new_lev  = s3.slider("Leverage Cap",      0.05, 0.30, float(gm.get("leverage_cap",      0.15)), 0.01)

            cb1, cb2 = st.columns(2)
            new_use_cash  = cb1.checkbox("Use cash before selling",  value=bool(rr.get("use_cash_before_selling",  True)))
            new_panic     = cb2.checkbox("Panic sell protection",     value=bool(rr.get("panic_sell_protection",     True)))
            new_avoid_tax = cb1.checkbox("Avoid taxable sales",       value=bool(rr.get("avoid_taxable_sales",       True)))
            new_trim_lev  = cb2.checkbox("Trim leverage before core", value=bool(rr.get("trim_leverage_before_core", True)))

            if st.form_submit_button("Save Portfolio Settings", type="primary"):
                cfg_raw["portfolio"]["cash_available"]       = new_cash
                cfg_raw["portfolio"]["monthly_contribution"] = new_contrib
                cfg_raw["growth_mode"]["mode"]               = new_mode
                cfg_raw["growth_mode"]["concentration_cap"]  = new_conc
                cfg_raw["growth_mode"]["leverage_cap"]       = new_lev
                cfg_raw["rebalance_rules"]["band_threshold"]             = new_band
                cfg_raw["rebalance_rules"]["use_cash_before_selling"]    = new_use_cash
                cfg_raw["rebalance_rules"]["panic_sell_protection"]      = new_panic
                cfg_raw["rebalance_rules"]["avoid_taxable_sales"]        = new_avoid_tax
                cfg_raw["rebalance_rules"]["trim_leverage_before_core"]  = new_trim_lev
                err = _save_config(cfg_raw)
                if not err:
                    st.success("Saved.")
                else:
                    st.error(f"Failed: {err}")

    # -- Holdings -------------------------------------------------------------
    with tab_hold:
        holdings = cfg_raw.get("portfolio", {}).get("holdings", [])
        st.subheader(f"{len(holdings)} Holdings")
        if holdings:
            st.dataframe(pd.DataFrame(holdings), width="stretch", hide_index=True)

        st.caption("Update share counts below. Add/remove symbols by editing config.json directly.")
        with st.form("form_holdings"):
            updated = []
            for h in holdings:
                hc1, hc2 = st.columns([3, 1])
                hc1.markdown(
                    f"**{h['symbol']}** -- {h['asset_class']} "
                    f"(target {float(h['target_weight']) * 100:.0f}%)"
                )
                new_sh = hc2.number_input("Shares", value=float(h.get("shares", 0)),
                                          step=1.0, format="%.2f", key=f"sh_{h['symbol']}")
                updated.append({**h, "shares": new_sh})

            if st.form_submit_button("Save Share Counts", type="primary"):
                cfg_raw["portfolio"]["holdings"] = updated
                err = _save_config(cfg_raw)
                if not err:
                    st.success("Saved.")
                else:
                    st.error(f"Failed: {err}")

    # -- Features & Services --------------------------------------------------
    with tab_feat:
        st.subheader("Feature Toggles")
        scanner = cfg_raw.get("scanner", {})
        sleeve  = cfg_raw.get("speculative_sleeve", {})
        theme   = cfg_raw.get("theme_engine", {})
        wl      = cfg_raw.get("watchlist_scanner", {})
        ml      = cfg_raw.get("ml_advisor", {})
        em      = cfg_raw.get("email", {})

        with st.form("form_features"):
            t1, t2 = st.columns(2)
            scan_on   = t1.checkbox("S&P 500 Scanner (FMP)",            value=bool(scanner.get("enabled", False)), help="Requires FMP_API_KEY")
            sleeve_on = t2.checkbox("Speculative Sleeve",                value=bool(sleeve.get("enabled",  False)))
            theme_on  = t1.checkbox("Theme Engine (RSS + LLM)",          value=bool(theme.get("enabled",   False)), help="Requires OpenAI (primary) or Anthropic (fallback)")
            wl_on     = t2.checkbox("Watchlist Scanner",                 value=bool(wl.get("enabled",      False)))
            ml_on     = t1.checkbox("ML Advisor",                        value=bool(ml.get("enabled",      True)))

            st.markdown("---")
            st.subheader("Email")
            email_on = st.checkbox("Email enabled", value=bool(em.get("enabled", False)))
            e1, e2   = st.columns(2)
            new_from = e1.text_input("Sender email",    value=str(em.get("sender_email",    "")))
            new_to   = e2.text_input("Recipient email", value=str(em.get("recipient_email", "")))

            st.markdown("---")
            st.subheader("Scanner Settings")
            ss1, ss2 = st.columns(2)
            new_topk    = ss1.slider("Top K watchlist",   10, 500, int(scanner.get("top_k_watchlist", 100)), 10)
            new_mincap  = ss2.number_input("Min Mkt Cap ($B)", value=float(scanner.get("min_mkt_cap", 5e9)) / 1e9, step=1.0, format="%.1f")
            new_mingrow = ss1.slider("Min Rev Growth",    0.0, 0.50, float(scanner.get("min_rev_growth", 0.15)), 0.01)
            trend_on    = ss2.checkbox("Trend filter (> 200 DMA)", value=bool(scanner.get("trend_filter_200dma", True)))

            if st.form_submit_button("Save Feature Settings", type="primary"):
                cfg_raw.setdefault("scanner", {}).update({
                    "enabled": scan_on, "top_k_watchlist": new_topk,
                    "min_mkt_cap": new_mincap * 1e9, "min_rev_growth": new_mingrow,
                    "trend_filter_200dma": trend_on,
                })
                cfg_raw.setdefault("speculative_sleeve", {})["enabled"] = sleeve_on
                cfg_raw.setdefault("theme_engine", {})["enabled"]       = theme_on
                cfg_raw.setdefault("watchlist_scanner", {})["enabled"]  = wl_on
                cfg_raw.setdefault("ml_advisor", {})["enabled"]         = ml_on
                cfg_raw.setdefault("email", {}).update({
                    "enabled": email_on, "sender_email": new_from, "recipient_email": new_to,
                })
                err = _save_config(cfg_raw)
                if not err:
                    st.success("Saved.")
                else:
                    st.error(f"Failed: {err}")

        st.divider()
        st.subheader("Watchlist Symbols (quick edit)")
        cur_symbols = wl.get("watchlist", [])
        new_wl_txt  = st.text_area("Comma-separated symbols", value=", ".join(cur_symbols), height=80)
        if st.button("Update Watchlist Symbols"):
            new_syms = [s.strip().upper() for s in new_wl_txt.split(",") if s.strip()]
            cfg_raw.setdefault("watchlist_scanner", {})["watchlist"] = new_syms
            err = _save_config(cfg_raw)
            if not err:
                st.success(f"Saved {len(new_syms)} symbols.")
            else:
                st.error(str(err))

    # -- Secrets --------------------------------------------------------------
    with tab_env:
        st.subheader("API Keys / Secrets")
        st.caption("Values are **never shown** -- only whether each key is configured.")

        for key, info in _env_status().items():
            k1, k2 = st.columns([4, 1])
            k1.markdown(f"**`{key}`** -- {info['desc']}")
            if info["set"]:
                k2.success("Set")
            else:
                k2.error("Missing")

        st.divider()
        if ENV_PATH.exists():
            st.success(f"`.env` found at `{ENV_PATH}`")
        else:
            st.error("`.env` not found -- copy `.env.template` and fill in your keys.")
            if ENV_TEMPLATE.exists() and st.button("Create .env from template"):
                ENV_PATH.write_text(ENV_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
                st.success(".env created. Open it and add your API keys, then reload.")


# ============================================================================
# PAGE: PROMPTS  (feature 5)
# ============================================================================

def page_prompts() -> None:
    st.title("Prompt Library")
    st.caption("Store and manage reusable prompts for this project. Saved to `data/prompts.json`.")

    CATEGORIES = [
        "Repo Summary", "Scanner Build", "Theme Detection",
        "GUI Generation", "Test / Debug", "Portfolio Analysis", "Other",
    ]

    prompts = _load_prompts()

    tab_browse, tab_add = st.tabs(["Browse & Edit", "Add New"])

    # -- Browse & Edit --------------------------------------------------------
    with tab_browse:
        if not prompts:
            st.info("No prompts yet. Add one in the 'Add New' tab.")
        else:
            cats_in_use = sorted(set(p.get("category", "Other") for p in prompts))
            sel_cat = st.selectbox(
                "Filter by category", ["(all)"] + cats_in_use, key="pr_cat_filter"
            )

            filtered = prompts if sel_cat == "(all)" else [
                p for p in prompts if p.get("category") == sel_cat
            ]
            st.caption(f"{len(filtered)} of {len(prompts)} prompts shown")

            for pr in filtered:
                real_idx = prompts.index(pr)
                with st.expander(
                    f"[{pr.get('category', 'Other')}]  {pr.get('title', 'Untitled')}",
                    expanded=False,
                ):
                    col_text, col_actions = st.columns([4, 1])
                    with col_text:
                        edited_title = st.text_input(
                            "Title", value=pr.get("title", ""), key=f"pr_title_{real_idx}"
                        )
                        cat_index = CATEGORIES.index(pr.get("category", "Other")) \
                            if pr.get("category", "Other") in CATEGORIES else 0
                        edited_cat = st.selectbox(
                            "Category", CATEGORIES, index=cat_index,
                            key=f"pr_cat_{real_idx}"
                        )
                        edited_text = st.text_area(
                            "Prompt text", value=pr.get("text", ""),
                            height=200, key=f"pr_text_{real_idx}"
                        )
                    with col_actions:
                        st.write("")
                        st.write("")
                        fname = pr.get("title", "prompt").replace(" ", "_") + ".txt"
                        st.download_button(
                            "Copy as .txt",
                            pr.get("text", "").encode("utf-8"),
                            file_name=fname,
                            key=f"pr_dl_{real_idx}",
                        )
                        if st.button("Save edits", key=f"pr_save_{real_idx}"):
                            prompts[real_idx].update({
                                "title":    edited_title,
                                "category": edited_cat,
                                "text":     edited_text,
                                "updated":  date.today().isoformat(),
                            })
                            err = _save_prompts(prompts)
                            if not err:
                                st.success("Saved.")
                            else:
                                st.error(err)

                        if st.button("Delete", key=f"pr_del_{real_idx}"):
                            prompts.pop(real_idx)
                            err = _save_prompts(prompts)
                            if not err:
                                st.success("Deleted.")
                                st.rerun()
                            else:
                                st.error(err)

    # -- Add New --------------------------------------------------------------
    with tab_add:
        with st.form("form_add_prompt"):
            new_title = st.text_input("Title", placeholder="e.g. Build S&P 500 Watchlist")
            new_cat   = st.selectbox("Category", CATEGORIES, key="pr_new_cat")
            new_text  = st.text_area(
                "Prompt text", height=250, placeholder="Paste or type your prompt here..."
            )

            if st.form_submit_button("Add Prompt", type="primary"):
                if not new_title.strip():
                    st.error("Title is required.")
                elif not new_text.strip():
                    st.error("Prompt text is required.")
                else:
                    safe_cat = new_cat.lower().replace(" ", "-").replace("/", "")
                    new_id   = f"{safe_cat}-{date.today()}-{len(prompts) + 1}"
                    prompts.append({
                        "id":       new_id,
                        "title":    new_title.strip(),
                        "category": new_cat,
                        "text":     new_text.strip(),
                        "created":  date.today().isoformat(),
                        "updated":  date.today().isoformat(),
                    })
                    err = _save_prompts(prompts)
                    if not err:
                        st.success(f"Added: {new_title}")
                        st.rerun()
                    else:
                        st.error(err)


# ============================================================================
# PAGE: LOGS  (feature 6 -- enhanced)
# ============================================================================

def page_logs() -> None:
    st.title("Logs")

    if not LOGS_DIR.exists():
        st.warning("`logs/` directory not found -- it is created on first run.")
        return

    log_files = sorted(LOGS_DIR.glob("*.log"), reverse=True)
    if not log_files:
        st.info("No log files yet.")
        return

    # File selector
    lf_col, tail_col = st.columns([3, 1])
    sel_log = lf_col.selectbox("Log file (newest first)", [f.name for f in log_files])
    tail_n  = tail_col.number_input("Last N lines", min_value=10, max_value=5000, value=200, step=50)

    lp = LOGS_DIR / sel_log
    st.caption(
        f"Path: `{lp}`  |  age: {_file_age(lp)}  |  size: {lp.stat().st_size:,} bytes"
    )

    all_lines = _read_text(lp).splitlines()

    # Filter controls
    fc1, fc2, fc3, fc4 = st.columns([3, 1, 1, 1])
    search_text  = fc1.text_input("Search text", placeholder="Filter lines...", key="log_search")
    errors_only  = fc2.checkbox("Errors only",  key="log_errors")
    warnings_too = fc3.checkbox("+ Warnings",   key="log_warnings", value=True,
                                help="Include WARNING lines when Errors only is active")
    newest_first = fc4.checkbox("Newest first", key="log_newest", value=True)

    # Apply tail
    display_lines = (
        all_lines[-int(tail_n):] if int(tail_n) < len(all_lines) else all_lines[:]
    )

    if newest_first:
        display_lines = list(reversed(display_lines))

    # Apply text search
    if search_text:
        lower = search_text.lower()
        display_lines = [ln for ln in display_lines if lower in ln.lower()]

    # Apply level filter
    if errors_only:
        level_terms = ["error", "exception", "traceback", "critical"]
        if warnings_too:
            level_terms += ["warning", "warn"]
        display_lines = [
            ln for ln in display_lines
            if any(t in ln.lower() for t in level_terms)
        ]

    # Stats
    err_count  = sum(1 for ln in all_lines if "error" in ln.lower() or "exception" in ln.lower())
    warn_count = sum(1 for ln in all_lines if "warning" in ln.lower())

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Total lines",   len(all_lines))
    s2.metric("Showing",       len(display_lines))
    s3.metric("Error lines",   err_count,
              delta=str(err_count)  if err_count  else None, delta_color="inverse")
    s4.metric("Warning lines", warn_count,
              delta=str(warn_count) if warn_count else None, delta_color="inverse")

    # Display
    st.code(
        "\n".join(display_lines) if display_lines else "(no matching lines)",
        language=None,
    )

    # Actions
    a1, a2, a3 = st.columns(3)
    with open(lp, "rb") as fh:
        a1.download_button(
            f"Download {sel_log}", fh.read(), file_name=sel_log, key="log_dl"
        )
    if a2.button("Clear view filters", key="log_clear_filters"):
        for k in ("log_search", "log_errors", "log_warnings", "log_newest"):
            st.session_state.pop(k, None)
        st.rerun()
    a3.markdown(f"`{lp}`")


# ============================================================================
# PAGE: DIAGNOSTICS  (feature 7 + 8 -- 2 new tabs)
# ============================================================================

def page_diagnostics() -> None:
    st.title("Diagnostics")
    tab_env, tab_data, tab_tests, tab_maint, tab_sched = st.tabs(
        ["Environment", "Data & Cache", "Tests", "Maintenance", "Scheduler"]
    )

    # -- Environment ----------------------------------------------------------
    with tab_env:
        st.subheader("Python Runtime")
        e1, e2 = st.columns(2)
        e1.code(f"Python {sys.version}", language=None)
        e2.code(str(PYTHON), language=None)

        st.subheader("Packages")
        for label, module in [
            ("streamlit",     "streamlit"),
            ("pandas",        "pandas"),
            ("requests",      "requests"),
            ("openpyxl",      "openpyxl"),
            ("python-dotenv", "dotenv"),
            ("feedparser",    "feedparser"),
            ("mcp",           "mcp"),
        ]:
            try:
                mod = __import__(module)
                ver = getattr(mod, "__version__", "installed")
                st.markdown(f"- **{label}** `{ver}` OK")
            except ImportError:
                st.markdown(f"- **{label}** -- NOT INSTALLED -- `pip install {label}`")

        st.subheader("API Keys")
        for key, info in _env_status().items():
            icon = "OK" if info["set"] else "MISSING"
            st.markdown(f"- **`{key}`** [{icon}] -- {info['desc']}")

        st.subheader("Config Validation")
        if st.button("Validate config.json", key="btn_validate"):
            rc, out = _run_command(
                [PYTHON, "-c",
                 "from utils import load_config, validate_config; "
                 "c = load_config(); issues = validate_config(c); "
                 "print('OK -- no issues' if not issues else '\\n'.join(issues))"],
                timeout=10,
            )
            if rc == 0 and "OK" in out:
                st.success(out.strip() or "(no output)")
            else:
                st.warning(out.strip() or "(no output)")

        st.subheader("LLM (OpenAI)")
        if st.button("Ping OpenAI", key="btn_llm_diag"):
            status = _get_llm_status(_load_config())
            if status["model_available"]:
                latency = status.get("latency_ms")
                latency_note = f" ({latency}ms)" if latency is not None else ""
                st.success(f"Reachable -- model `{status['model']}`{latency_note}")
            else:
                st.warning(f"Not reachable -- {status['error'] or 'unknown error'}")

        st.subheader("Run Lock")
        lock = DATA_DIR / "run.lock"
        if lock.exists():
            age_s = (datetime.now() - datetime.fromtimestamp(lock.stat().st_mtime)).total_seconds()
            mins  = int(age_s // 60)
            if age_s > 1800:
                st.warning(f"Stale lock file ({mins}m old) -- a previous run may have crashed.")
                if st.button("Remove lock", key="btn_rmlock"):
                    lock.unlink(missing_ok=True)
                    st.success("Lock removed.")
                    st.rerun()
            else:
                st.info(f"Lock file exists ({mins}m old) -- run in progress?")
        else:
            st.success("No lock file -- system is idle.")

    # -- Data & Cache ---------------------------------------------------------
    with tab_data:
        st.subheader("Key Data Files")
        data_files = {
            "config.json":               CONFIG_PATH,
            ".env":                      ENV_PATH,
            "data/portfolio.db":         DATA_DIR / "portfolio.db",
            "data/price_cache.json":     DATA_DIR / "price_cache.json",
            "data/drawdown_state.json":  DATA_DIR / "drawdown_state.json",
            "data/last_success.json":    DATA_DIR / "last_success.json",
            "data/ml_history.json":      DATA_DIR / "ml_history.json",
            "data/rss_seen.json":        DATA_DIR / "rss_seen.json",
            "data/finance_history.json": DATA_DIR / "finance_history.json",
            "data/prompts.json":         PROMPTS_PATH,
            "data/watchlist_tags.json":  WL_TAGS_PATH,
        }
        rows = []
        for name, path in data_files.items():
            if path.exists():
                rows.append({"File": name, "Size": f"{path.stat().st_size:,} B",
                             "Age": _file_age(path), "Status": "OK"})
            else:
                rows.append({"File": name, "Size": "--", "Age": "--", "Status": "MISSING"})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

        st.subheader("Output Directories")
        for dp, label in [
            (OUTPUTS_LATEST,               "outputs/latest/"),
            (OUTPUTS_HISTORY,              "outputs/history/"),
            (LOGS_DIR,                     "logs/"),
            (DATA_DIR / "watchlist_cache", "data/watchlist_cache/"),
            (DATA_DIR / "fmp_cache",       "data/fmp_cache/"),
        ]:
            if dp.exists():
                n   = sum(1 for f in dp.rglob("*") if f.is_file())
                kbs = sum(f.stat().st_size for f in dp.rglob("*") if f.is_file()) / 1024
                st.markdown(f"- **`{label}`** -- {n} files, {kbs:.1f} KB")
            else:
                st.markdown(f"- **`{label}`** -- not found")

        st.subheader("Cache Operations")
        ca1, ca2, ca3 = st.columns(3)

        if ca1.button("Clear price cache"):
            p = DATA_DIR / "price_cache.json"
            if p.exists():
                p.unlink()
                st.success("Price cache cleared.")
            else:
                st.info("Price cache does not exist.")

        if ca2.button("Clear watchlist cache"):
            wlc = DATA_DIR / "watchlist_cache"
            if wlc.exists():
                files = list(wlc.glob("*.json"))
                for f in files:
                    f.unlink(missing_ok=True)
                st.success(f"Removed {len(files)} watchlist cache files.")
            else:
                st.info("Watchlist cache not found.")

        if ca3.button("Clear RSS seen cache"):
            p = DATA_DIR / "rss_seen.json"
            if p.exists():
                p.unlink()
                st.success("RSS seen cache cleared.")
            else:
                st.info("rss_seen.json does not exist.")

        st.subheader("SQLite Run History (last 20)")
        db_rows = _query_db(
            "SELECT run_id, mode, status, started_at, completed_at "
            "FROM run_history ORDER BY started_at DESC LIMIT 20"
        )
        if db_rows:
            st.dataframe(pd.DataFrame(db_rows), hide_index=True, width="stretch")
        else:
            st.info("No run history yet.")

        st.subheader("Portfolio Peaks (DB)")
        peaks = _query_db("SELECT * FROM portfolio_peaks ORDER BY recorded_at DESC LIMIT 5")
        if peaks:
            st.dataframe(pd.DataFrame(peaks), hide_index=True, width="stretch")
        else:
            st.info("No portfolio_peaks records yet.")

    # -- Tests ----------------------------------------------------------------
    with tab_tests:
        st.subheader("Full Test Suite")
        st.caption("`python -m unittest discover tests/ -v`  (185 tests, ~1 skipped)")

        if st.button("Run All Tests", type="primary", key="btn_all_tests"):
            with st.spinner("Running... (up to 120 s)"):
                rc, out = _run_command(
                    [PYTHON, "-m", "unittest", "discover", "tests/", "-v"], timeout=120
                )
            if rc == 0:
                st.success("All tests passed!")
            else:
                st.error(f"Tests failed (exit {rc})")

            for line in reversed(out.splitlines()):
                if any(line.startswith(p) for p in ("Ran ", "OK", "FAILED", "ERROR")):
                    st.info(line)
                    break

            st.text_area("Full output", out, height=400, key="all_tests_out")

        st.divider()
        st.subheader("Individual Test Files")
        test_files = sorted(TESTS_DIR.glob("test_*.py")) if TESTS_DIR.exists() else []
        if not test_files:
            st.info("No test files in tests/")
        else:
            for tf in test_files:
                tc1, tc2 = st.columns([4, 1])
                tc1.markdown(f"`{tf.name}`")
                if tc2.button("Run", key=f"btn_{tf.stem}"):
                    with st.spinner(f"Running {tf.name}..."):
                        rc, out = _run_command(
                            [PYTHON, "-m", "unittest", f"tests.{tf.stem}", "-v"], timeout=60
                        )
                    if rc == 0:
                        st.success(f"`{tf.name}` passed")
                    else:
                        st.error(f"`{tf.name}` failed (exit {rc})")
                    st.text_area("Output", out, height=200, key=f"out_{tf.stem}")

    # -- Maintenance ----------------------------------------------------------
    with tab_maint:
        st.subheader("Quick Checks")
        m1, m3 = st.columns(2)

        with m1:
            st.markdown("**Config**")
            if st.button("Validate config.json", key="btn_maint_validate"):
                rc, out = _run_command(
                    [PYTHON, "-c",
                     "from utils import load_config, validate_config; "
                     "c = load_config(); issues = validate_config(c); "
                     "print('OK -- no issues' if not issues else '\\n'.join(issues))"],
                    timeout=10,
                )
                if rc == 0 and "OK" in out:
                    st.success(out.strip() or "(no output)")
                else:
                    st.warning(out.strip() or "(no output)")

        with m3:
            st.markdown("**LLM (OpenAI)**")
            if st.button("Test OpenAI", key="btn_maint_llm"):
                status = _get_llm_status(_load_config())
                if status["model_available"]:
                    st.success(f"Reachable -- model `{status['model']}`")
                else:
                    st.error(f"Not reachable: {status['error'] or 'unknown error'}")

        st.divider()
        st.subheader("Open Folders")
        of1, of2 = st.columns(2)

        with of1:
            st.markdown(f"**outputs/latest/**  \n`{OUTPUTS_LATEST}`")
            if st.button("Open in Explorer", key="btn_open_latest"):
                if OUTPUTS_LATEST.exists():
                    subprocess.Popen(["explorer", str(OUTPUTS_LATEST)])
                    st.success("Opened.")
                else:
                    st.warning("Directory does not exist yet.")

        with of2:
            st.markdown(f"**outputs/history/**  \n`{OUTPUTS_HISTORY}`")
            if st.button("Open in Explorer", key="btn_open_hist"):
                if OUTPUTS_HISTORY.exists():
                    subprocess.Popen(["explorer", str(OUTPUTS_HISTORY)])
                    st.success("Opened.")
                else:
                    st.warning("Directory does not exist yet.")

        logs_col, data_col = st.columns(2)
        with logs_col:
            st.markdown(f"**logs/**  \n`{LOGS_DIR}`")
            if st.button("Open logs/ in Explorer", key="btn_open_logs"):
                if LOGS_DIR.exists():
                    subprocess.Popen(["explorer", str(LOGS_DIR)])
                    st.success("Opened.")
                else:
                    st.warning("logs/ does not exist yet.")

        with data_col:
            st.markdown(f"**Project root**  \n`{ROOT}`")
            if st.button("Open project root in Explorer", key="btn_open_root"):
                subprocess.Popen(["explorer", str(ROOT)])
                st.success("Opened.")

        st.divider()
        st.subheader("Export Latest Results")
        if OUTPUTS_LATEST.exists() and any(OUTPUTS_LATEST.iterdir()):
            if st.button("Build .zip of outputs/latest/", key="btn_build_zip"):
                with st.spinner("Zipping..."):
                    zdata = _zip_latest_outputs()
                st.download_button(
                    "Download outputs_latest.zip",
                    zdata,
                    file_name=f"outputs_latest_{date.today()}.zip",
                    mime="application/zip",
                    key="btn_dl_zip",
                )
        else:
            st.info("No files in outputs/latest/ yet.")

        st.divider()
        st.subheader("Clear All Caches")
        st.caption("Clears price cache, watchlist cache, and RSS seen cache. Safe to run anytime.")
        if st.button("Clear ALL caches", key="btn_clear_all", type="secondary"):
            cleared = []
            for fp in [DATA_DIR / "price_cache.json", DATA_DIR / "rss_seen.json"]:
                if fp.exists():
                    fp.unlink()
                    cleared.append(fp.name)
            wlc = DATA_DIR / "watchlist_cache"
            if wlc.exists():
                files = list(wlc.glob("*.json"))
                for f in files:
                    f.unlink(missing_ok=True)
                cleared.append(f"watchlist_cache/ ({len(files)} files)")
            if cleared:
                st.success(f"Cleared: {', '.join(cleared)}")
            else:
                st.info("Nothing to clear.")

    # -- Scheduler ------------------------------------------------------------
    with tab_sched:
        st.subheader("Run Mode Commands")
        st.caption(
            "This system is designed for Windows Task Scheduler, cron, or manual execution. "
            "Schedule the commands below as recurring tasks."
        )

        modes_info = [
            {
                "Mode":        "daily",
                "Command":     "python main.py --run-mode daily --skip-email",
                "Recommended": "Weekdays 08:00",
                "Behavior":    "Silent unless action required; uses cached prices < 24h",
                "API calls":   "0-2 (AV price cache)",
            },
            {
                "Mode":        "weekly",
                "Command":     "python main.py --run-mode weekly",
                "Recommended": "Sunday morning",
                "Behavior":    "Full digest + S&P 500 watchlist refresh",
                "API calls":   "~3 FMP (scanner enabled)",
            },
            {
                "Mode":        "monthly",
                "Command":     "python main.py --run-mode monthly",
                "Recommended": "1st of month",
                "Behavior":    "Capital Deployment Memo + full scan + Claude monthly memo",
                "API calls":   "~8 FMP + 1 Claude",
            },
        ]
        st.dataframe(pd.DataFrame(modes_info), width="stretch", hide_index=True)

        st.subheader("Sub-system Commands")
        st.code(
            "# Watchlist Scanner\n"
            f"{PYTHON} -m watchlist_scanner\n\n"
            "# Theme Engine (requires an LLM provider: OpenAI primary / Anthropic fallback)\n"
            f"{PYTHON} -m theme_engine --mode daily\n\n"
            "# AI Agent\n"
            f"{PYTHON} -m agent --mode daily\n"
            f"{PYTHON} -m agent --mode daily --no-network   # offline -- no LLM needed",
            language="bash",
        )

        st.subheader("Last Known Run Times (database)")
        db_rows = _query_db(
            "SELECT mode, status, MAX(started_at) as last_run, completed_at "
            "FROM run_history GROUP BY mode ORDER BY last_run DESC"
        )
        if db_rows:
            st.dataframe(pd.DataFrame(db_rows), width="stretch", hide_index=True)
        else:
            st.info("No run history in database yet.")

        st.subheader("Windows Task Scheduler")
        if st.button("Query schtasks", key="btn_schtasks"):
            with st.spinner("Querying Task Scheduler..."):
                rc, out = _run_command(["schtasks", "/query", "/fo", "LIST", "/v"], timeout=15)
            if rc == 0:
                # Try to find stockbot-related tasks
                lines = out.splitlines()
                relevant, block = [], []
                for ln in lines:
                    block.append(ln)
                    if not ln.strip() and block:
                        block_text = "\n".join(block)
                        if any(
                            kw in block_text.lower()
                            for kw in ["stockbot", "stock_bot", "main.py"]
                        ):
                            relevant.extend(block)
                        block = []

                if relevant:
                    st.code("\n".join(relevant), language=None)
                else:
                    st.info("No stockbot-related tasks found in Task Scheduler.")
                    with st.expander("Full schtasks output (first 3000 chars)"):
                        st.code(out[:3000], language=None)
            else:
                st.warning(f"schtasks failed (exit {rc}). May require elevated permissions.")

        st.subheader("Create Task (PowerShell)")
        st.markdown(
            "Run this in an **admin PowerShell** to schedule the daily analysis at 08:00 on weekdays:"
        )
        py_path   = str(PYTHON).replace("\\", "\\\\")
        main_path = str(ROOT / "main.py").replace("\\", "\\\\")
        st.code(
            f'schtasks /create /tn "StockBotDaily" '
            f'/tr "\\"{py_path}\\" \\"{main_path}\\" --run-mode daily --skip-email" '
            f'/sc WEEKLY /d MON,TUE,WED,THU,FRI /st 08:00 /f',
            language="powershell",
        )


# ============================================================================
# Operator Cockpit — Reusable UI helpers (card-based, beginner-friendly)
# ============================================================================
#
# These helpers are intentionally small, dependency-light, and read-only.
# They never write artifacts, never call broker/API endpoints, and never use
# trading-instruction language ("buy"/"sell"/"hold") outside the fixed
# safety disclaimer wording.
#
# Color semantics:
#   green / success    → healthy, monitor, supported
#   yellow / warning   → needs review, weak evidence, partial coverage
#   red / error        → rejected, high risk, safety violation
#   gray / info        → expired, no data, neutral
# ----------------------------------------------------------------------------

_COCKPIT_DISCLAIMER = (
    "This is sandbox research governance only. "
    "It is not a buy/sell/hold recommendation."
)


def _status_tone(status: str) -> str:
    """Map an automatic-promotion status to a card tone."""
    s = str(status or "").strip().upper()
    if s == "MONITOR":
        return "good"
    if s in ("NEEDS_REVIEW", "WATCH"):
        return "warn"
    if s == "REJECTED":
        return "bad"
    if s == "EXPIRED":
        return "neutral"
    return "neutral"


def _status_explanation(status: str) -> str:
    """Plain-English one-liner for an automatic-promotion status."""
    s = str(status or "").strip().upper()
    return {
        "MONITOR": "Strong enough to keep watching",
        "NEEDS_REVIEW": "Mixed evidence — operator should inspect",
        "REJECTED": "Risk or weak evidence",
        "EXPIRED": "No longer supported by recent evidence",
        "WATCH": "Sandbox watch candidate",
        "DISCOVERED": "Early-stage research candidate",
    }.get(s, "Sandbox research only")


def render_status_badge(text: str, tone: str = "neutral") -> str:
    """Public helper alias for ``_badge``; returns inline HTML."""
    return _badge(text, tone)


def render_metric_card(
    title: str,
    value: str,
    subtitle: str = "",
    badges: list[str] | None = None,
) -> None:
    """Public helper alias for ``_render_operator_card``."""
    _render_operator_card(title=title, value=value, subtitle=subtitle, badges=badges)


def render_section_header(title: str, subtitle: str = "") -> None:
    """Render a section header with optional caption."""
    st.markdown(f"### {title}")
    if subtitle:
        st.caption(subtitle)


def render_empty_state(message: str = "Nothing here yet.", icon: str = "ℹ️") -> None:
    """Render a friendly empty-state info panel."""
    st.info(f"{icon} {message}")


def render_safety_flags(safety_flags: dict, missing: list[str] | None = None) -> None:
    """
    Render the safety boundary panel.

    Shows one row of green / red badges, one per expected safety flag.
    Emits a warning if any flag is missing or False.
    """
    if not isinstance(safety_flags, dict) or not safety_flags:
        render_empty_state("No safety flags reported by the producing artifact.", "⚠️")
        return
    badges: list[str] = []
    for flag, value in safety_flags.items():
        tone = "good" if bool(value) else "bad"
        badges.append(_badge(f"{flag}: {'✓' if value else '✗'}", tone))
    st.markdown("".join(badges), unsafe_allow_html=True)
    if missing:
        st.warning(
            "Some safety flags are missing or False: "
            + ", ".join(missing[:9])
        )


def render_candidate_card(decision: dict, key_prefix: str = "ap") -> None:
    """
    Render one automatic-promotion candidate as an expandable card.

    Beginner-friendly: shows ticker, status, plain-English explanation,
    evidence numbers, and a short reason.  Expander reveals full evidence
    details and raw JSON.
    """
    if not isinstance(decision, dict):
        return
    ticker = str(decision.get("ticker") or "—").upper()
    status = str(decision.get("proposed_status") or "—").upper()
    tone = _status_tone(status)
    explanation = _status_explanation(status)
    evidence_score = decision.get("evidence_score", 0.0)
    corroboration = decision.get("corroboration_score", 0.0)
    news_relevance = decision.get("news_relevance_score", 0.0)
    sources = decision.get("source_diversity", 0)
    reason = str(decision.get("reason") or "—")
    risk_flags = decision.get("risk_flags") or []
    catalyst_flags = decision.get("catalyst_flags") or []

    badges = [
        _badge(status, tone),
        _badge(f"evidence {evidence_score}", "neutral"),
    ]
    if isinstance(risk_flags, list) and risk_flags:
        badges.append(_badge(f"{len(risk_flags)} risk", "bad"))
    if isinstance(catalyst_flags, list) and catalyst_flags:
        badges.append(_badge(f"{len(catalyst_flags)} catalyst", "good"))

    _render_operator_card(
        title=f"{ticker} — {explanation}",
        value=str(ticker),
        subtitle=reason[:180],
        badges=badges,
    )

    with st.expander(f"Details · {ticker}", expanded=False):
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Evidence", f"{evidence_score}")
        col2.metric("Corroboration", f"{corroboration}")
        col3.metric("News relevance", f"{news_relevance}")
        col4.metric("Sources", f"{sources}")

        st.markdown("**Why this candidate moved:**")
        st.write(reason)

        gates_passed = decision.get("gates_passed") or []
        gates_failed = decision.get("gates_failed") or []
        if gates_passed or gates_failed:
            st.markdown("**Gates:**")
            st.write({
                "passed": gates_passed,
                "failed": gates_failed,
            })

        if risk_flags:
            st.markdown("**Risk flags (research context):**")
            st.write(list(risk_flags))
        if catalyst_flags:
            st.markdown("**Catalyst flags (research context):**")
            st.write(list(catalyst_flags))

        for label, key in (
            ("Replay context", "replay_context"),
            ("Memory context", "memory_context"),
            ("Operator context", "operator_context"),
            ("Evidence summary", "evidence_summary"),
        ):
            val = decision.get(key)
            if val:
                st.markdown(f"**{label}:** {val}")

        st.caption("Raw decision record (read-only):")
        st.json(decision)


def _render_cockpit_summary_grid(bundle: dict) -> None:
    """
    Operator cockpit summary — 7 card grid at the top of the Dashboard.

    Read-only.  Reads existing artifacts already loaded into ``bundle`` by
    ``load_operator_dashboard_data()``.  Cards degrade gracefully when an
    upstream artifact is missing.  Never uses trading-instruction language
    outside fixed safety disclaimers.

    Cards:
      1. Portfolio Status
      2. Today's Market Narrative
      3. Decision Plan
      4. Data Quality
      5. News Evidence
      6. Automatic Promotion
      7. Memo Delivery
    """
    if not isinstance(bundle, dict):
        bundle = {}

    decision_plan = bundle.get("decision_plan") if isinstance(bundle.get("decision_plan"), dict) else {}
    sys_summary   = bundle.get("system_decision_summary") if isinstance(bundle.get("system_decision_summary"), dict) else {}
    dq            = bundle.get("data_quality_report") if isinstance(bundle.get("data_quality_report"), dict) else {}
    nel           = bundle.get("news_evidence_layer") if isinstance(bundle.get("news_evidence_layer"), dict) else {}
    nar           = bundle.get("market_narrative_daily") if isinstance(bundle.get("market_narrative_daily"), dict) else {}
    apg           = bundle.get("automatic_promotion") if isinstance(bundle.get("automatic_promotion"), dict) else {}
    memo          = bundle.get("memo_delivery_status") if isinstance(bundle.get("memo_delivery_status"), dict) else {}

    # --- Card 1: Portfolio Status ----------------------------------------
    health = str(sys_summary.get("system_health") or sys_summary.get("overall_health") or "")
    if health.lower() in ("healthy", "ok", "good"):
        port_tone = "good"
    elif health.lower() in ("degraded", "warn", "warning"):
        port_tone = "warn"
    elif health.lower() in ("critical", "fail", "failed"):
        port_tone = "bad"
    else:
        port_tone = "neutral"
    port_value = health.title() if health else "—"
    port_subtitle = "Current pipeline + data health snapshot"

    # --- Card 2: Today's Market Narrative --------------------------------
    nar_headline = str(nar.get("top_headline") or "").strip()
    nar_available = bool(nar.get("available")) and bool(nar_headline)
    nar_value = nar_headline[:80] if nar_available else "—"
    nar_subtitle = "Daily market narrative (read-only context)"
    nar_tone = "good" if nar_available else "neutral"

    # --- Card 3: Decision Plan -------------------------------------------
    decisions = decision_plan.get("decisions") if isinstance(decision_plan.get("decisions"), list) else []
    decision_count = len(decisions)
    dp_value = str(decision_count)
    dp_subtitle = "Positions covered by the latest decision plan"
    dp_tone = "good" if decision_count > 0 else "neutral"

    # --- Card 4: Data Quality --------------------------------------------
    dq_health = str(dq.get("overall_health") or dq.get("health_status") or "")
    issue_count = 0
    if isinstance(dq.get("issues"), list):
        issue_count = len(dq["issues"])
    if dq_health.lower() == "healthy":
        dq_tone = "good"
        dq_value = "Healthy"
    elif dq_health.lower() in ("degraded", "warning"):
        dq_tone = "warn"
        dq_value = "Degraded"
    elif dq_health.lower() in ("critical", "failed"):
        dq_tone = "bad"
        dq_value = "Critical"
    elif issue_count > 0:
        dq_tone = "warn"
        dq_value = f"{issue_count} issue(s)"
    else:
        dq_tone = "neutral"
        dq_value = "—"
    dq_subtitle = "Observe-only data quality monitor"

    # --- Card 5: News Evidence -------------------------------------------
    nel_available = bool(nel.get("available"))
    ticker_contexts = nel.get("ticker_contexts") if isinstance(nel.get("ticker_contexts"), list) else []
    nel_value = str(len(ticker_contexts)) if nel_available else "—"
    nel_subtitle = "Per-ticker news evidence (context only)"
    nel_tone = "good" if nel_available and ticker_contexts else "neutral"

    # --- Card 6: Automatic Promotion -------------------------------------
    apg_available = bool(apg.get("available"))
    apg_monitor = int(apg.get("monitor_count") or 0)
    apg_review = int(apg.get("needs_review_count") or 0)
    if apg_available:
        apg_value = f"{apg_monitor} monitor"
        if apg_review:
            apg_value += f" · {apg_review} review"
        apg_tone = "good" if apg_monitor > 0 else ("warn" if apg_review > 0 else "neutral")
    else:
        apg_value = "—"
        apg_tone = "neutral"
    apg_subtitle = "Sandbox research governance"

    # --- Card 7: Memo Delivery -------------------------------------------
    memo_available = bool(memo.get("available", memo.get("enabled") is not None))
    memo_sent = bool(memo.get("sent"))
    memo_skipped = bool(memo.get("skipped"))
    memo_disabled = (memo.get("enabled") is False) or (str(memo.get("reason") or "").lower() == "disabled")
    if memo_disabled:
        memo_value = "Disabled"
        memo_tone = "neutral"
    elif memo_sent:
        memo_value = "Sent"
        memo_tone = "good"
    elif memo_skipped:
        memo_value = "Skipped"
        memo_tone = "neutral"
    elif memo_available:
        memo_value = "Pending"
        memo_tone = "warn"
    else:
        memo_value = "—"
        memo_tone = "neutral"
    memo_subtitle = "Daily memo email status"

    # --- Render -----------------------------------------------------------
    render_section_header(
        "Cockpit Summary",
        "At-a-glance snapshot of the latest observe-only artifacts. "
        "Read-only — this view does not change portfolio state.",
    )
    row1 = st.columns(4)
    with row1[0]:
        render_metric_card(
            "Portfolio Status", port_value, port_subtitle,
            [render_status_badge("system", port_tone)],
        )
    with row1[1]:
        render_metric_card(
            "Today's Market Narrative", nar_value, nar_subtitle,
            [render_status_badge("narrative", nar_tone)],
        )
    with row1[2]:
        render_metric_card(
            "Decision Plan", dp_value, dp_subtitle,
            [render_status_badge("decisions", dp_tone)],
        )
    with row1[3]:
        render_metric_card(
            "Data Quality", dq_value, dq_subtitle,
            [render_status_badge("quality", dq_tone)],
        )

    row2 = st.columns(4)
    with row2[0]:
        render_metric_card(
            "News Evidence", nel_value, nel_subtitle,
            [render_status_badge("news", nel_tone)],
        )
    with row2[1]:
        render_metric_card(
            "Automatic Promotion", apg_value, apg_subtitle,
            [render_status_badge("sandbox", apg_tone)],
        )
    with row2[2]:
        render_metric_card(
            "Memo Delivery", memo_value, memo_subtitle,
            [render_status_badge("memo", memo_tone)],
        )
    with row2[3]:
        # Final card: safety reminder so the operator always sees the boundary
        render_metric_card(
            "Safety Boundary",
            "Observe-only",
            "No trades. No portfolio mutation. No buy/sell/hold recommendation.",
            [render_status_badge("safe", "good")],
        )


def page_automatic_promotion() -> None:
    """
    Operator Cockpit — Automatic Promotion Review.

    Read-only sandbox research view.  Reads
    outputs/sandbox/discovery/automatic_promotion_* artifacts.
    Never writes or mutates state.  Never emits trading instructions.
    """
    _operator_dashboard_css()
    render_section_header(
        "Automatic Promotion Review",
        "Sandbox research only — how recent discovery candidates were "
        "automatically classified by governance gates.",
    )
    st.markdown(f"> **{_COCKPIT_DISCLAIMER}**")

    data = load_automatic_promotion_data(ROOT)

    if not data.get("available"):
        render_empty_state(
            "No automatic promotion artifacts found yet. Run the governance "
            "layer in DISCOVERY or BACKTEST mode to populate this page.",
            icon="🧪",
        )
        st.caption(
            "Producer: portfolio_automation.discovery.automatic_promotion_governance"
        )
        return

    # --- Top metrics row ---------------------------------------------------
    cols = st.columns(6)
    with cols[0]:
        render_metric_card(
            "Total Reviewed",
            str(data["decision_count"]),
            "Candidates evaluated this run",
            [_badge("sandbox", "neutral")],
        )
    with cols[1]:
        render_metric_card(
            "Moved to Monitor",
            str(data["monitor_count"]),
            "Strong enough to keep watching",
            [_badge("monitor", "good")],
        )
    with cols[2]:
        render_metric_card(
            "Needs Review",
            str(data["needs_review_count"]),
            "Mixed evidence — operator should inspect",
            [_badge("review", "warn")],
        )
    with cols[3]:
        render_metric_card(
            "Rejected",
            str(data["rejected_count"]),
            "Risk or weak evidence",
            [_badge("rejected", "bad")],
        )
    with cols[4]:
        render_metric_card(
            "Expired",
            str(data["expired_count"]),
            "No longer supported by recent evidence",
            [_badge("expired", "neutral")],
        )
    with cols[5]:
        ok = data.get("safety_flags_ok")
        render_metric_card(
            "Safety Status",
            "OK" if ok else "Check",
            "All required flags hardcoded true" if ok else "Some flags missing",
            [_badge("safe" if ok else "warning", "good" if ok else "bad")],
        )

    st.divider()

    # --- Safety boundary panel --------------------------------------------
    render_section_header(
        "Safety Boundary",
        "These flags are hardcoded by the producer and re-checked here.",
    )
    render_safety_flags(
        data.get("safety_flags") or {},
        data.get("missing_safety_flags") or [],
    )

    st.divider()

    # --- Plain-English explanation -----------------------------------------
    with st.expander("What does each status mean?", expanded=False):
        st.markdown(
            "- **Monitor** — *Strong enough to keep watching.* Corroboration, "
            "news relevance, and persistence all met the governance gates. "
            "Still sandbox research; no investment action implied.\n"
            "- **Needs Review** — *Mixed evidence — operator should inspect.* "
            "Some gates passed but not all. The operator decides what (if "
            "anything) to do next.\n"
            "- **Rejected** — *Risk or weak evidence.* Either the risk flag "
            "count exceeded the maximum, or the candidate was already in the "
            "sandbox rejected list, or upstream carried a forbidden status.\n"
            "- **Expired** — *No longer supported by recent evidence.* No "
            "discovery memory signal within the staleness window.\n"
            "\n"
            "**Why this is not a trade recommendation:** the automatic "
            "promotion layer is capped at `context_only`. It cannot alter "
            "official portfolio, watchlist, allocation, scoring, "
            "recommendation, or decision state."
        )

    # --- Grouped candidate sections ---------------------------------------
    by_status = data.get("candidates_by_status") or {}
    section_order = (
        ("MONITOR", "Candidates Moved To Monitor",
         "Strong enough to keep watching."),
        ("NEEDS_REVIEW", "Candidates Needing Review",
         "Mixed evidence — operator should inspect."),
        ("REJECTED", "Candidates Rejected",
         "Risk or weak evidence."),
        ("EXPIRED", "Candidates Expired",
         "No longer supported by recent evidence."),
    )

    for status_key, header, caption in section_order:
        items = by_status.get(status_key) or []
        st.divider()
        render_section_header(header, caption)
        if not items:
            render_empty_state(
                f"No candidates currently in {status_key.replace('_', ' ').title()}.",
                icon="—",
            )
            continue
        for idx, item in enumerate(items):
            render_candidate_card(item, key_prefix=f"{status_key.lower()}_{idx}")

    # --- Producer summary markdown ----------------------------------------
    summary_md = data.get("summary_markdown") or ""
    if summary_md.strip():
        st.divider()
        render_section_header(
            "Producer-rendered summary",
            "Verbatim Markdown from the automatic_promotion_summary.md artifact.",
        )
        with st.expander("Show summary Markdown", expanded=False):
            st.markdown(summary_md)

    # --- Recent decision log (JSONL) --------------------------------------
    recent = data.get("recent_decisions") or []
    if recent:
        st.divider()
        render_section_header(
            "Recent decisions (audit log)",
            "Last 50 lines of automatic_promotion_decisions.jsonl.",
        )
        with st.expander("Show recent decision records", expanded=False):
            st.json(recent)

    # --- Gates table ------------------------------------------------------
    gates = data.get("gates") or {}
    if gates:
        st.divider()
        render_section_header(
            "Governance gates in effect",
            "Tunable thresholds that controlled this run.",
        )
        with st.expander("Show gate values", expanded=False):
            st.json(gates)

    # --- Footer -----------------------------------------------------------
    st.divider()
    st.caption(
        f"Generated at: `{data.get('generated_at') or '—'}` · "
        f"Run mode: `{data.get('run_mode') or '—'}` · "
        f"Run id: `{data.get('run_id') or '—'}`"
    )
    st.caption(
        "Source artifacts (read-only): "
        "`outputs/sandbox/discovery/automatic_promotion_candidates.json` · "
        "`outputs/sandbox/discovery/automatic_promotion_summary.md` · "
        "`outputs/sandbox/discovery/automatic_promotion_decisions.jsonl`"
    )


# ============================================================================
# ROUTER
# ============================================================================

if   page == "Dashboard":    page_dashboard()
elif page == "Decision Center": page_decision_center()
elif page == "Run Controls": page_run_controls()
elif page == "Outputs":      page_outputs()
elif page == "Watchlist":    page_watchlist_manager()
elif page == "Run History":  page_run_history()
elif page == "API Health":   page_api_health()
elif page == "Config Editor":page_config_editor()
elif page == "Prompts":      page_prompts()
elif page == "Logs":         page_logs()
elif page == "Diagnostics":  page_diagnostics()
elif page == "Automatic Promotion": page_automatic_promotion()
elif page == "Production Health":
    from gui.production_health_page import render_production_health_page
    render_production_health_page(ROOT)
