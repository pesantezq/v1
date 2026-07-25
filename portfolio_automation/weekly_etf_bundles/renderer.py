"""
Weekly ETF bundle digest rendering — Markdown + HTML.

Both formats are derived from ONE shared section model (`build_sections`) so they
cannot disagree. Observe-only language and the market-data date always appear.
Missing values render as "n/a" — never as 0. The system track record is shown
with real numbers ONLY when the scorecard sample is sufficient; otherwise it is
explicitly withheld.
"""
from __future__ import annotations

import html
from typing import Any

from portfolio_automation import weekly_etf_bundles as _pkg

_BANNER = ("OBSERVE-ONLY — informational watchlist. No brokerage trade execution. "
           "Does not create trades or feed production scoring.")


def _pct(x: Any, digits: int = 2) -> str:
    if x is None:
        return "n/a"
    try:
        return f"{float(x) * 100:+.{digits}f}%"
    except (TypeError, ValueError):
        return "n/a"


def _num(x: Any, digits: int = 3) -> str:
    if x is None:
        return "n/a"
    try:
        return f"{float(x):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def build_sections(analysis_payload: dict[str, Any],
                   scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ordered, format-agnostic section model for the digest."""
    p = analysis_payload
    bundles = sorted(p.get("bundles", []),
                     key=lambda b: (b.get("bundle_score") is None, -(b.get("bundle_score") or 0)))
    ctx = p.get("market_context", {}) or {}

    # Executive summary: leaders/laggards among bundles.
    ranked_named = [(b.get("name"), b.get("bundle_score"), b.get("state")) for b in bundles]
    top = [n for n, s, _ in ranked_named if s is not None][:3]

    warnings: list[str] = []
    if p.get("stale_symbols"):
        warnings.append(f"Stale price data: {', '.join(p['stale_symbols'])}")
    if p.get("failed_symbols"):
        warnings.append(f"No data for: {', '.join(p['failed_symbols'])}")
    if p.get("panel_missing_symbols"):
        warnings.append(f"Missing from price archive: {', '.join(p['panel_missing_symbols'])}")
    if (p.get("coverage") or 0) < 0.8:
        warnings.append(f"Overall coverage {_pct(p.get('coverage'), 0)} is below 80%.")

    return {
        "banner": _BANNER,
        "generated_at": p.get("generated_at"),
        "market_data_date": p.get("market_data_date"),
        "bundle_count": p.get("bundle_count", 0),
        "etf_count": p.get("etf_count", 0),
        "market_regime": ctx.get("market_regime", "unknown"),
        "volatility_regime": ctx.get("volatility_regime", "unknown"),
        "executive_top_bundles": top,
        "bundles": bundles,
        "warnings": warnings,
        "track_record": _track_record(scorecard),
        "disclaimer": _pkg.DISCLAIMER,
    }


def _track_record(scorecard: dict[str, Any] | None) -> dict[str, Any]:
    if not scorecard:
        return {"available": False, "reason": "no_scorecard"}
    status = scorecard.get("sample_status")
    if status != "sufficient":
        return {"available": False, "reason": f"sample_{status}",
                "matured_prediction_count": scorecard.get("matured_prediction_count", 0)}
    return {
        "available": True,
        "primary_horizon": scorecard.get("primary_horizon"),
        "matured_prediction_count": scorecard.get("matured_prediction_count"),
        "benchmark_relative_hit_rate": scorecard.get("benchmark_relative_hit_rate"),
        "precision_at_3": scorecard.get("precision_at_3"),
        "avg_excess_return": scorecard.get("avg_excess_return"),
        "top_bottom_score_spread": scorecard.get("top_bottom_score_spread"),
        "information_coefficient": scorecard.get("information_coefficient"),
    }


def render_subject(analysis_payload: dict[str, Any]) -> str:
    return f"Weekly ETF Bundle Watchlist — {analysis_payload.get('market_data_date', 'n/a')}"


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def render_weekly_md(analysis_payload: dict[str, Any],
                     scorecard: dict[str, Any] | None = None) -> str:
    s = build_sections(analysis_payload, scorecard)
    L: list[str] = []
    a = L.append
    a(f"# Weekly ETF Bundle Watchlist — {s['market_data_date']}")
    a("")
    a(f"> **{s['banner']}**")
    a("")
    a(f"**Generated:** {s['generated_at']}  ")
    a(f"**Market data through:** {s['market_data_date']}  ")
    a(f"**Bundles:** {s['bundle_count']} · **ETFs:** {s['etf_count']}  ")
    a(f"**Market regime:** {s['market_regime']} · **Volatility regime:** {s['volatility_regime']}")
    a("")
    a("## Executive Summary")
    if s["executive_top_bundles"]:
        a("Leading bundles: " + ", ".join(s["executive_top_bundles"]) + ".")
    else:
        a("_No bundle scores available this week._")
    a("")

    for b in s["bundles"]:
        a(f"## {b.get('name')}  ·  score {_num(b.get('bundle_score'), 1)}  ·  _{b.get('state')}_")
        a("")
        a(f"- Benchmark: `{b.get('benchmark')}` · 4w excess vs benchmark: {_pct(b.get('excess_return_12w'))} (12w)")
        a(f"- Breadth: {_pct(b.get('pct_above_sma50'), 0)} > 50d · "
          f"{_pct(b.get('pct_above_sma200'), 0)} > 200d · "
          f"{_pct(b.get('pct_positive_momentum_4w'), 0)} positive 4w momentum")
        a(f"- Concentration: {_num(b.get('leadership_concentration'), 2)} · "
          f"score dispersion: {_num(b.get('score_dispersion'), 1)} · "
          f"weekly Δ: {_num(b.get('weekly_score_change'), 1)}")
        strongest, weakest = b.get("strongest"), b.get("weakest")
        if strongest:
            a(f"- Strongest: `{strongest}` · Weakest: `{weakest}`")
        a("")
        a("| Rank | ETF | Role | Score | Label | 4w | 12w-excess |")
        a("|---|---|---|---|---|---|---|")
        members = sorted(b.get("members", []),
                         key=lambda m: (m.get("watch_score") is None, -(m.get("watch_score") or 0)))
        for m in members:
            met = m.get("metrics", {})
            a(f"| {m.get('rank_in_bundle', '—')} | `{m.get('symbol')}` | {m.get('role', '')} | "
              f"{m.get('watch_score') if m.get('watch_score') is not None else 'n/a'} | "
              f"{m.get('label')} | {_pct(met.get('return_4w'))} | {_pct(met.get('excess_return_12w'))} |")
        a("")

    if s["warnings"]:
        a("## Data-Quality Warnings")
        for w in s["warnings"]:
            a(f"- {w}")
        a("")

    a("## System Track Record")
    tr = s["track_record"]
    if tr.get("available"):
        a(f"- Primary evaluation window: {tr['primary_horizon']}")
        a(f"- Matured predictions: {tr['matured_prediction_count']}")
        a(f"- Benchmark-relative hit rate: {_pct(tr['benchmark_relative_hit_rate'], 1)}")
        a(f"- Top-three precision: {_pct(tr['precision_at_3'], 1)}")
        a(f"- Average excess return: {_pct(tr['avg_excess_return'])}")
        a(f"- Top-vs-bottom score spread: {_pct(tr['top_bottom_score_spread'])}")
        a(f"- Information coefficient: {_num(tr['information_coefficient'], 3)}")
        a("")
        a("_Statistics are observational and do not represent live portfolio returns._")
    else:
        a(f"_Track record withheld — sample not yet sufficient "
          f"({tr.get('matured_prediction_count', 0)} matured predictions)._")
    a("")
    a("---")
    a(f"_{s['disclaimer']}_")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# HTML (self-contained, inline styles)
# --------------------------------------------------------------------------- #
def render_weekly_html(analysis_payload: dict[str, Any],
                       scorecard: dict[str, Any] | None = None) -> str:
    s = build_sections(analysis_payload, scorecard)
    e = html.escape
    H: list[str] = []
    a = H.append
    a('<div style="font-family:Arial,Helvetica,sans-serif;max-width:760px;margin:auto;color:#111">')
    a(f'<div style="background:#fff3cd;border:1px solid #ffe08a;padding:10px 14px;'
      f'border-radius:6px;font-weight:bold">{e(s["banner"])}</div>')
    a(f'<h1 style="font-size:20px">Weekly ETF Bundle Watchlist — {e(str(s["market_data_date"]))}</h1>')
    a(f'<p style="color:#555;font-size:13px">Generated: {e(str(s["generated_at"]))}<br>'
      f'Market data through: <b>{e(str(s["market_data_date"]))}</b><br>'
      f'Bundles: {s["bundle_count"]} · ETFs: {s["etf_count"]}<br>'
      f'Market regime: {e(str(s["market_regime"]))} · Volatility regime: {e(str(s["volatility_regime"]))}</p>')

    a("<h2 style='font-size:16px'>Executive Summary</h2>")
    if s["executive_top_bundles"]:
        a(f"<p>Leading bundles: {e(', '.join(s['executive_top_bundles']))}.</p>")
    else:
        a("<p><i>No bundle scores available this week.</i></p>")

    for b in s["bundles"]:
        a(f'<h2 style="font-size:16px">{e(str(b.get("name")))} · score {e(_num(b.get("bundle_score"),1))} '
          f'· <i>{e(str(b.get("state")))}</i></h2>')
        a(f'<p style="font-size:13px;color:#333">Benchmark: <code>{e(str(b.get("benchmark")))}</code> · '
          f'12w excess: {e(_pct(b.get("excess_return_12w")))} · '
          f'breadth &gt;50d {e(_pct(b.get("pct_above_sma50"),0))}, &gt;200d {e(_pct(b.get("pct_above_sma200"),0))} · '
          f'concentration {e(_num(b.get("leadership_concentration"),2))}</p>')
        a('<table style="border-collapse:collapse;width:100%;font-size:13px">')
        a('<tr style="background:#f0f0f0"><th align="left">Rank</th><th align="left">ETF</th>'
          '<th align="left">Role</th><th align="right">Score</th><th align="left">Label</th>'
          '<th align="right">4w</th><th align="right">12w-excess</th></tr>')
        members = sorted(b.get("members", []),
                         key=lambda m: (m.get("watch_score") is None, -(m.get("watch_score") or 0)))
        for m in members:
            met = m.get("metrics", {})
            score = m.get("watch_score")
            a(f'<tr><td>{e(str(m.get("rank_in_bundle","—")))}</td><td><code>{e(str(m.get("symbol")))}</code></td>'
              f'<td>{e(str(m.get("role","")))}</td><td align="right">{score if score is not None else "n/a"}</td>'
              f'<td>{e(str(m.get("label")))}</td><td align="right">{e(_pct(met.get("return_4w")))}</td>'
              f'<td align="right">{e(_pct(met.get("excess_return_12w")))}</td></tr>')
        a("</table>")

    if s["warnings"]:
        a("<h2 style='font-size:16px'>Data-Quality Warnings</h2><ul>")
        for w in s["warnings"]:
            a(f"<li>{e(w)}</li>")
        a("</ul>")

    a("<h2 style='font-size:16px'>System Track Record</h2>")
    tr = s["track_record"]
    if tr.get("available"):
        a("<ul>")
        a(f"<li>Primary evaluation window: {e(str(tr['primary_horizon']))}</li>")
        a(f"<li>Matured predictions: {tr['matured_prediction_count']}</li>")
        a(f"<li>Benchmark-relative hit rate: {e(_pct(tr['benchmark_relative_hit_rate'],1))}</li>")
        a(f"<li>Top-three precision: {e(_pct(tr['precision_at_3'],1))}</li>")
        a(f"<li>Average excess return: {e(_pct(tr['avg_excess_return']))}</li>")
        a(f"<li>Top-vs-bottom score spread: {e(_pct(tr['top_bottom_score_spread']))}</li>")
        a(f"<li>Information coefficient: {e(_num(tr['information_coefficient'],3))}</li>")
        a("</ul><p style='font-size:12px;color:#555'><i>Statistics are observational and do not "
          "represent live portfolio returns.</i></p>")
    else:
        a(f"<p><i>Track record withheld — sample not yet sufficient "
          f"({tr.get('matured_prediction_count',0)} matured predictions).</i></p>")

    a(f'<hr><p style="font-size:12px;color:#777">{e(s["disclaimer"])}</p>')
    a("</div>")
    return "".join(H)
