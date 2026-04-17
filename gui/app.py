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
from agent.llm_adapters import resolve_ollama_base_url, validate_ollama_connection
from gui_operator_data import load_operator_dashboard_data
from tools.weekly_report import generate_weekly_summary, markdown_to_plain_text

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
AV_CALL_BUDGET   = 20   # Alpha Vantage free-tier budget (watchlist scanner)

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
        "ALPHA_VANTAGE_API_KEY": "Market data -- required for price fetch",
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
            st.dataframe(df, use_container_width=True)
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
        if st.button("Open in Outputs", key=button_key, use_container_width=True):
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
        st.dataframe(df, use_container_width=True, hide_index=True)


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
            use_container_width=True,
        )

    fp = file_map[sel]
    st.caption(f"`{fp.relative_to(ROOT)}` -- {_file_age(fp)} -- {fp.stat().st_size:,} bytes")
    _render_file(fp)
    with open(fp, "rb") as fh:
        st.download_button(f"Download {sel}", fh.read(), file_name=sel)


def _render_overview_mode(bundle: dict) -> None:
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
            st.dataframe(triage_df, use_container_width=True, hide_index=True)
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
        st.dataframe(freshness_df, use_container_width=True, hide_index=True)

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
    st.dataframe(freshness_df, use_container_width=True, hide_index=True)

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
                "Normalized Allocation": _fmt_ratio_pct(row["normalized_allocation"]),
                "Sector": row["sector"],
                "Cooldown": "Yes" if row["cooldown_active"] else "No",
                "Degraded Impact": _fmt_ratio_pct(row["degraded_impact"]),
                "Reliability": row["signal_reliability"],
                "Actionable": "Yes" if row["actionable_signal"] else "No",
            }
            for row in filtered_rows
        ]
    )
    st.dataframe(triage_df, use_container_width=True, hide_index=True)

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
            st.dataframe(alt_df, use_container_width=True, hide_index=True)
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
            st.dataframe(alt_df, use_container_width=True, hide_index=True)
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
    st.dataframe(artifact_df, use_container_width=True, hide_index=True)


def _render_performance_tab(bundle: dict) -> None:
    performance = bundle["performance_view"]
    st.subheader("Performance")
    _render_interpretation("Higher confidence should correspond to higher hit rates. Sample sizes are shown on every table and flagged when thin.")
    if not performance["available"]:
        st.info("No data available.")
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
            use_container_width=True,
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
        st.info("No data available.")
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
        use_container_width=True,
        hide_index=True,
    )
    for note in regime_view.get("notes", []):
        st.caption(note)


def _render_recommendation_quality_tab(bundle: dict) -> None:
    quality = bundle["recommendation_quality_view"]
    st.subheader("Recommendation Quality")
    _render_interpretation("Higher scores and higher confidence should trend toward better outcomes over time, but small samples can easily distort the picture.")
    if not quality["available"]:
        st.info("No data available.")
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
            use_container_width=True,
            hide_index=True,
        )

    for note in quality.get("notes", []):
        st.caption(note)


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
        if st.button("Regenerate", key="weekly_review_regenerate", use_container_width=True):
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
                use_container_width=True,
            )
        with export_cols[1]:
            st.download_button(
                "Download Plain Text",
                plain_text,
                file_name="weekly_summary.txt",
                mime="text/plain",
                use_container_width=True,
            )


# -- v2 helpers --------------------------------------------------------------

def _av_budget() -> dict:
    """Read AV call counter. Returns date, count, budget, remaining, cache_stats."""
    counter = _load_json(WL_CALL_COUNTER)
    today = date.today().isoformat()
    count = counter.get("count", 0) if counter.get("date") == today else 0

    wlc = DATA_DIR / "watchlist_cache"
    cache_stats = {"daily": 0, "news": 0, "overview": 0, "quote": 0}
    if wlc.exists():
        for f in wlc.glob("*.json"):
            n = f.name
            if n.startswith("daily_"):
                cache_stats["daily"] += 1
            elif n.startswith("news_"):
                cache_stats["news"] += 1
            elif n.startswith("overview_"):
                cache_stats["overview"] += 1
            elif n.startswith("quote_"):
                cache_stats["quote"] += 1

    return {
        "date":        today,
        "count":       count,
        "budget":      AV_CALL_BUDGET,
        "remaining":   max(0, AV_CALL_BUDGET - count),
        "cache_stats": cache_stats,
    }


def _test_av_connection():
    """
    Live connectivity test via GLOBAL_QUOTE (costs 1 API call).
    Returns (status, message) where status is 'ok' | 'warning' | 'error'.
    """
    key = _get_api_key("ALPHA_VANTAGE_API_KEY")
    if not key or key.startswith("your_"):
        return "error", "ALPHA_VANTAGE_API_KEY not set -- add it to .env"
    url = (
        "https://www.alphavantage.co/query"
        f"?function=GLOBAL_QUOTE&symbol=SPY&apikey={key}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "stockbot/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        if "Note" in data:
            return "warning", f"Rate-limited: {data['Note']}"
        if "Information" in data:
            return "warning", f"API info: {data['Information']}"
        if "Global Quote" in data and data["Global Quote"]:
            price = data["Global Quote"].get("05. price", "?")
            return "ok", f"Connected -- SPY = ${price}"
        return "warning", f"Unexpected response keys: {list(data.keys())}"
    except Exception as exc:
        return "error", f"Connection failed: {exc}"


def _get_ollama_status(cfg: dict) -> dict:
    """
    Check Ollama: running, configured model, model availability.
    Returns dict: running, base_url, model, model_available, available_models, error.
    """
    base_url = (
        os.environ.get("OLLAMA_BASE_URL", "")
        or cfg.get("theme_engine", {}).get("ollama_base_url", "")
        or "http://localhost:11434/v1"
    )
    model = (
        os.environ.get("OLLAMA_MODEL", "")
        or cfg.get("theme_engine", {}).get("ollama_model", "gemma3:4b")
    )
    result = {
        "running": False, "base_url": base_url, "model": model,
        "model_available": False, "available_models": [], "error": "",
        "timed_out": False, "timeout_seconds": 20,
    }
    try:
        timeout = max(
            5,
            int(
                os.environ.get(
                    "OLLAMA_HEALTH_TIMEOUT",
                    cfg.get("theme_engine", {}).get("ollama_health_timeout_seconds", 20),
                )
            ),
        )
        normalized_base_url = resolve_ollama_base_url(base_url)
        check = validate_ollama_connection(
            model=model,
            base_url=normalized_base_url,
            timeout=timeout,
        )
        result["base_url"] = normalized_base_url
        result["available_models"] = check.get("available_models", [])
        result["timeout_seconds"] = timeout
        result["model_available"] = check.get("ok", False)
        message = str(check.get("message", "") or "")
        message_lower = message.lower()
        result["timed_out"] = "timed out" in message_lower
        result["running"] = (
            bool(check.get("ok"))
            or "not installed" in message_lower
            or bool(result["available_models"])
            or result["timed_out"]
        )
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
# SIDEBAR
# ============================================================================

cfg      = _load_config()
last_ok  = _load_json(DATA_DIR / "last_success.json")
drawdown = _load_json(DATA_DIR / "drawdown_state.json")

st.sidebar.title("StockBot")
st.sidebar.caption("Operator Dashboard")

PAGES = [
    "Dashboard", "Run Controls", "Outputs",
    "Watchlist", "Run History", "API Health",
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
    bundle = load_operator_dashboard_data(ROOT)

    title_col, action_col = st.columns([5, 1])
    with title_col:
        st.title("Operator Dashboard")
        st.caption(
            "Artifact-driven visibility into the latest advisory outputs. "
            "Missing files degrade gracefully and the dashboard does not alter live investing behavior."
        )
    with action_col:
        if st.button("Refresh", use_container_width=True):
            st.rerun()

    mode = st.radio(
        "Dashboard mode",
        ["Overview", "Advanced"],
        horizontal=True,
        key="operator_dashboard_mode",
    )

    if mode == "Overview":
        _render_overview_mode(bundle)
        return

    tab_status, tab_memo, tab_triage, tab_portfolio, tab_strategy, tab_health, tab_performance, tab_regime, tab_quality, tab_weekly = st.tabs(
        [
            "Run Status",
            "Memo Review",
            "Signal Triage",
            "Portfolio Construction",
            "Strategy Recommendation",
            "Health / Reliability",
            "Performance",
            "Regime",
            "Recommendation Quality",
            "Weekly Review",
        ]
    )

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
    with tab_quality:
        _render_recommendation_quality_tab(bundle)
    with tab_weekly:
        _render_weekly_review_tab(bundle)


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
    run_daily   = m1.button("Run Daily",   use_container_width=True, type="primary",
                            help="Alert-only digest; uses cached prices if < 24 h old.")
    run_weekly  = m2.button("Run Weekly",  use_container_width=True,
                            help="Full digest + S&P 500 watchlist refresh (~3 FMP calls).")
    run_monthly = m3.button("Run Monthly", use_container_width=True,
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
                                    help="--no-network: templated memo, no Ollama/Claude needed.")
        if st.button("Run AI Agent", use_container_width=True, key="btn_agent"):
            cmd = [PYTHON, "-m", "agent", "--mode", agent_mode]
            if agent_offline:
                cmd.append("--no-network")
            with st.spinner(f"Running agent ({agent_mode})..."):
                rc, out = _run_command(cmd, timeout=180)
            _store_run(rc, out, f"AI Agent ({agent_mode})")

    with s2:
        st.markdown("**Watchlist Scanner** (`python -m watchlist_scanner`)")
        wl_dry = st.checkbox("Dry run", value=True, key="wl_dry",
                             help="Uses cached data only -- no Alpha Vantage calls.")
        if st.button("Run Watchlist Scanner", use_container_width=True, key="btn_wl"):
            cmd = [PYTHON, "-m", "watchlist_scanner"]
            if wl_dry:
                cmd.append("--dry-run")
            with st.spinner("Running watchlist scanner..."):
                rc, out = _run_command(cmd, timeout=120)
            _store_run(rc, out, "Watchlist Scanner")

    with s3:
        st.markdown("**Theme Engine** (`python -m theme_engine`)")
        theme_mode = st.selectbox("Mode", ["daily", "weekly", "monthly"], key="th_mode")
        if st.button("Run Theme Engine", use_container_width=True, key="btn_theme"):
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
            st.dataframe(df, use_container_width=True, hide_index=True)
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
                    st.success(f"Saved metadata for {sel_sym}.") if not err else st.error(err)

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
            st.success(f"Saved {len(new_syms)} symbols.") if not err else st.error(err)

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
        st.dataframe(display, use_container_width=True, hide_index=True)
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
    st.dataframe(pd.DataFrame(hist_summary), use_container_width=True, hide_index=True)

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
        st.dataframe(pd.DataFrame(peaks), use_container_width=True, hide_index=True)
    else:
        st.info("No portfolio_peaks records yet.")


# ============================================================================
# PAGE: API HEALTH  (feature 1 + 2)
# ============================================================================

def page_api_health() -> None:
    st.title("API & Model Health")
    cfg_raw = _load_config()

    # -- Alpha Vantage --------------------------------------------------------
    st.subheader("Alpha Vantage")
    av_key_set = _env_status().get("ALPHA_VANTAGE_API_KEY", {}).get("set", False)

    if av_key_set:
        st.success("API key: **set**")
    else:
        st.error("API key: **missing** -- add `ALPHA_VANTAGE_API_KEY` to `.env`")

    if av_key_set:
        if st.button("Test connectivity (costs 1 API call)", key="btn_av_test"):
            with st.spinner("Testing..."):
                status, msg = _test_av_connection()
            if status == "ok":
                st.success(f"Healthy: {msg}")
            elif status == "warning":
                st.warning(f"Warning: {msg}")
            else:
                st.error(f"Error: {msg}")
                st.info(
                    "Check that your API key is valid at alphavantage.co. "
                    "Free-tier keys can take a few minutes to activate after registration."
                )
    else:
        st.caption("Connectivity test disabled until key is configured.")

    # -- Usage Budget ---------------------------------------------------------
    st.subheader("Daily API Budget (Alpha Vantage)")
    budget = _av_budget()
    used   = budget["count"]
    limit  = budget["budget"]
    remain = budget["remaining"]

    b1, b2, b3 = st.columns(3)
    b1.metric("Used today", used)
    b2.metric("Budget",     limit)
    b3.metric("Remaining",  remain,
              delta=str(remain),
              delta_color="normal" if remain > 5 else "inverse")

    pct = used / limit if limit > 0 else 0
    st.progress(min(pct, 1.0), text=f"{used}/{limit} calls ({pct:.0%})")

    if used >= limit:
        st.error(
            "Daily budget exhausted -- watchlist scanner is blocked until midnight. "
            "Consider running with --dry-run or clearing the call counter."
        )
    elif remain <= 3:
        st.warning(
            f"Only {remain} calls remaining today. "
            "Dry-run mode recommended to avoid exceeding the limit."
        )

    st.caption(
        f"Budget date: {budget['date']}  |  "
        f"Counter file: `data/watchlist_cache/call_counter.json`  |  "
        f"Budget source: `watchlist_scanner/config.py:MAX_DAILY_CALLS`"
    )

    # Endpoint breakdown
    cs = budget["cache_stats"]
    if any(cs.values()):
        st.subheader("Cache File Breakdown")
        cache_df = pd.DataFrame([
            {"Endpoint": "Daily OHLCV",         "TTL": "24h",  "Cached files": cs["daily"]},
            {"Endpoint": "News / Sentiment",     "TTL": "4h",   "Cached files": cs["news"]},
            {"Endpoint": "Company Overview",     "TTL": "7d",   "Cached files": cs["overview"]},
            {"Endpoint": "Real-time Quote",      "TTL": "30m",  "Cached files": cs["quote"]},
        ])
        st.dataframe(cache_df, use_container_width=True, hide_index=True)
        st.caption(
            "Cached responses serve future requests without consuming budget. "
            "Clear caches in Diagnostics > Maintenance to force fresh API fetches."
        )
    else:
        st.info("No cache files found yet. Run Watchlist Scanner to populate cache.")

    st.divider()

    # -- Ollama ---------------------------------------------------------------
    st.subheader("Ollama (local LLM)")
    ollama = _get_ollama_status(cfg_raw)

    o1, o2 = st.columns(2)
    o1.markdown(f"**Base URL:** `{ollama['base_url']}`")
    o2.markdown(f"**Configured model:** `{ollama['model']}`")

    if ollama["running"]:
        st.success("Ollama: **running**")
        if ollama["model_available"]:
            st.success(f"Model `{ollama['model']}`: **available**")
        elif ollama.get("timed_out"):
            st.warning(
                f"Model `{ollama['model']}` is installed, but the dashboard health check timed out after "
                f"{ollama.get('timeout_seconds', 20)}s.  \n"
                "Ollama is reachable, but this model is responding slowly right now.  \n"
                "Fix: raise `OLLAMA_HEALTH_TIMEOUT`, use a lighter local model, or retry once the model is warm."
            )
        else:
            st.warning(
                f"Model `{ollama['model']}` is **not installed**.  \n"
                f"Fix: `ollama pull {ollama['model']}`  \n"
                f"Available models: "
                + (", ".join(f"`{m}`" for m in ollama["available_models"]) or "_(none installed)_")
            )
        if ollama["available_models"]:
            with st.expander("All installed models"):
                for m in ollama["available_models"]:
                    st.markdown(f"- `{m}`")
    else:
        st.error(
            f"Ollama: **not reachable**  \n"
            f"Error: `{ollama['error']}`  \n"
            "Fix: start Ollama with `ollama serve`, or install from https://ollama.ai"
        )
        st.info(
            "Ollama is only required for the Theme Engine and AI Agent (daily/weekly modes). "
            "Portfolio analysis runs without it."
        )

    st.divider()

    # -- Other API keys -------------------------------------------------------
    st.subheader("Other API Keys")
    for key, info in _env_status().items():
        if key == "ALPHA_VANTAGE_API_KEY":
            continue
        row1, row2 = st.columns([4, 1])
        row1.markdown(f"**`{key}`** -- {info['desc']}")
        row2.success("Set") if info["set"] else row2.error("Missing")

    st.divider()

    # -- Network checks -------------------------------------------------------
    st.subheader("Network Connectivity")
    nc1, nc2 = st.columns(2)

    if nc1.button("Ping alphavantage.co", key="btn_av_dns"):
        try:
            with urllib.request.urlopen("https://www.alphavantage.co", timeout=5) as r:
                st.success(f"alphavantage.co reachable (HTTP {r.status})")
        except Exception as exc:
            st.error(f"alphavantage.co unreachable: {exc}")

    if nc2.button("Ping api.anthropic.com", key="btn_claude_dns"):
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
                st.success("Saved.") if not err else st.error(f"Failed: {err}")

    # -- Holdings -------------------------------------------------------------
    with tab_hold:
        holdings = cfg_raw.get("portfolio", {}).get("holdings", [])
        st.subheader(f"{len(holdings)} Holdings")
        if holdings:
            st.dataframe(pd.DataFrame(holdings), use_container_width=True, hide_index=True)

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
                st.success("Saved.") if not err else st.error(f"Failed: {err}")

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
            theme_on  = t1.checkbox("Theme Engine (RSS + Ollama)",       value=bool(theme.get("enabled",   False)), help="Requires local Ollama")
            wl_on     = t2.checkbox("Watchlist Scanner (Alpha Vantage)", value=bool(wl.get("enabled",      False)))
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
                st.success("Saved.") if not err else st.error(f"Failed: {err}")

        st.divider()
        st.subheader("Watchlist Symbols (quick edit)")
        cur_symbols = wl.get("watchlist", [])
        new_wl_txt  = st.text_area("Comma-separated symbols", value=", ".join(cur_symbols), height=80)
        if st.button("Update Watchlist Symbols"):
            new_syms = [s.strip().upper() for s in new_wl_txt.split(",") if s.strip()]
            cfg_raw.setdefault("watchlist_scanner", {})["watchlist"] = new_syms
            err = _save_config(cfg_raw)
            st.success(f"Saved {len(new_syms)} symbols.") if not err else st.error(str(err))

    # -- Secrets --------------------------------------------------------------
    with tab_env:
        st.subheader("API Keys / Secrets")
        st.caption("Values are **never shown** -- only whether each key is configured.")

        for key, info in _env_status().items():
            k1, k2 = st.columns([4, 1])
            k1.markdown(f"**`{key}`** -- {info['desc']}")
            k2.success("Set") if info["set"] else k2.error("Missing")

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
                            st.success("Saved.") if not err else st.error(err)

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
            (st.success if rc == 0 and "OK" in out else st.warning)(out.strip() or "(no output)")

        st.subheader("Ollama")
        if st.button("Ping Ollama", key="btn_ollama_diag"):
            try:
                with urllib.request.urlopen("http://localhost:11434/api/version", timeout=4) as r:
                    data = json.loads(r.read())
                st.success(f"Running -- version `{data.get('version', 'unknown')}`")
            except Exception as exc:
                st.warning(f"Not reachable -- {exc}")

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
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

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
            st.dataframe(pd.DataFrame(db_rows), hide_index=True, use_container_width=True)
        else:
            st.info("No run history yet.")

        st.subheader("Portfolio Peaks (DB)")
        peaks = _query_db("SELECT * FROM portfolio_peaks ORDER BY recorded_at DESC LIMIT 5")
        if peaks:
            st.dataframe(pd.DataFrame(peaks), hide_index=True, use_container_width=True)
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
                    (st.success if rc == 0 else st.error)(
                        f"`{tf.name}` {'passed' if rc == 0 else f'failed (exit {rc})'}"
                    )
                    st.text_area("Output", out, height=200, key=f"out_{tf.stem}")

    # -- Maintenance ----------------------------------------------------------
    with tab_maint:
        st.subheader("Quick Checks")
        m1, m2, m3 = st.columns(3)

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
                (st.success if rc == 0 and "OK" in out else st.warning)(
                    out.strip() or "(no output)"
                )

        with m2:
            st.markdown("**Alpha Vantage**")
            if st.button("Test AV connection", key="btn_maint_av"):
                with st.spinner("Testing..."):
                    status, msg = _test_av_connection()
                {"ok": st.success, "warning": st.warning, "error": st.error}[status](msg)

        with m3:
            st.markdown("**Ollama**")
            if st.button("Test Ollama", key="btn_maint_ollama"):
                try:
                    with urllib.request.urlopen(
                        "http://localhost:11434/api/version", timeout=4
                    ) as r:
                        d = json.loads(r.read())
                    st.success(f"Running -- v{d.get('version', '?')}")
                except Exception as exc:
                    st.error(f"Not reachable: {exc}")

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
            st.success(f"Cleared: {', '.join(cleared)}") if cleared else st.info("Nothing to clear.")

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
        st.dataframe(pd.DataFrame(modes_info), use_container_width=True, hide_index=True)

        st.subheader("Sub-system Commands")
        st.code(
            "# Watchlist Scanner (uses Alpha Vantage budget)\n"
            f"{PYTHON} -m watchlist_scanner\n\n"
            "# Theme Engine (requires Ollama)\n"
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
            st.dataframe(pd.DataFrame(db_rows), use_container_width=True, hide_index=True)
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
# ROUTER
# ============================================================================

if   page == "Dashboard":    page_dashboard()
elif page == "Run Controls": page_run_controls()
elif page == "Outputs":      page_outputs()
elif page == "Watchlist":    page_watchlist_manager()
elif page == "Run History":  page_run_history()
elif page == "API Health":   page_api_health()
elif page == "Config Editor":page_config_editor()
elif page == "Prompts":      page_prompts()
elif page == "Logs":         page_logs()
elif page == "Diagnostics":  page_diagnostics()
