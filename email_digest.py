"""
Finance Email Digest Module

Generates structured, signal-optimized email reports based on
scored recommendations with anti-spam controls.

Enhanced sections (Phase 2 upgrade):
  A. Top 3 Actions
  C. What Changed Since Last Run
  D. Trajectory / Long-Term Outlook
  E. Opportunity Cost of Idle Cash
  F+G. Behavior Guardrails & Hold Signal
  H. Position Rationale
  I. System Status
"""

import hashlib
import json as _json
import logging
import smtplib
import ssl
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING, Optional, List, Dict

from utils import get_env, format_currency, format_percent
from scoring import (
    FinanceRecommendation, ActionLevel, ImpactArea,
    categorize_recommendations, filter_for_email, should_send_email,
    deduplicate_recommendations
)

if TYPE_CHECKING:
    from digest_builder import DigestContext


logger = logging.getLogger('portfolio_automation.email_digest')


class EmailDigestError(Exception):
    """Custom exception for email digest errors."""
    pass


def build_email_subject(categorized: Dict[ActionLevel, List[FinanceRecommendation]]) -> str:
    """Build email subject line with counts."""
    action_req = len(categorized[ActionLevel.ACTION_REQUIRED])
    recommended = len(categorized[ActionLevel.RECOMMENDED])
    monitor = len(categorized[ActionLevel.MONITOR])
    
    parts = []
    if action_req:
        parts.append(f"{action_req} Action Required")
    if recommended:
        parts.append(f"{recommended} Recommended")
    if monitor:
        parts.append(f"{monitor} Monitor")
    
    if not parts:
        return f"Finance Digest - {date.today().strftime('%Y-%m-%d')}"
    
    return f"Finance Digest: {' • '.join(parts)}"


def build_top_summary(
    total_portfolio: float,
    cash_available: float,
    savings_rate: Optional[float] = None,
    target_savings_rate: Optional[float] = None,
    emergency_months: Optional[float] = None,
    target_emergency_months: Optional[float] = None,
    max_drift: Optional[float] = None,
    drift_band: float = 0.07
) -> List[str]:
    """Build top summary lines (max 4)."""
    lines = []
    
    # Portfolio value
    lines.append(f"Portfolio: {format_currency(total_portfolio)} | Cash: {format_currency(cash_available)}")
    
    # Savings rate
    if savings_rate is not None and target_savings_rate is not None:
        status = "✅" if savings_rate >= target_savings_rate else "⚠️"
        lines.append(f"Savings rate: {savings_rate:.0%} (target {target_savings_rate:.0%}) {status}")
    
    # Emergency fund
    if emergency_months is not None and target_emergency_months is not None:
        status = "✅" if emergency_months >= target_emergency_months else "⚠️"
        lines.append(f"Emergency fund: {emergency_months:.1f} months (target {target_emergency_months:.0f}-6) {status}")
    
    # Portfolio drift
    if max_drift is not None:
        status = "✅" if abs(max_drift) <= drift_band else "⚠️"
        lines.append(f"Portfolio drift: {abs(max_drift):.1%} (band +/-{drift_band:.0%}) {status}")
    
    return lines[:4]


def format_action_required_item(rec: FinanceRecommendation) -> str:
    """Format a single Action Required item (3 bullets)."""
    return f"""**{rec.title}** (Score: {rec.final_score})
• What: {rec.what_changed}
• Why: {rec.why_it_matters}
• Do: {rec.action}"""


def format_recommended_item(rec: FinanceRecommendation) -> str:
    """Format a single Recommended item (one line)."""
    return f"• {rec.title} — {rec.action} — Next: {rec.next_check}"


def format_monitor_item(rec: FinanceRecommendation) -> str:
    """Format a single Monitor item."""
    return f"• {rec.title}: {rec.what_changed}"


def build_text_body(
    categorized: Dict[ActionLevel, List[FinanceRecommendation]],
    summary_lines: List[str],
    timestamp: str,
    context: Optional['DigestContext'] = None,
) -> str:
    """Build plain text email body, including enhanced sections when context is provided."""
    from digest_builder import (
        build_top3_actions, build_what_changed, build_trajectory,
        build_opportunity_cost, build_behavior_section,
        build_holding_rationale, build_system_status,
        build_theme_highlights,
    )

    lines = []

    # Header
    lines.append("=" * 52)
    lines.append("  PORTFOLIO DIGEST")
    lines.append("=" * 52)
    lines.append("")

    # A. Top 3 Actions (always first when context available)
    if context is not None:
        top3 = build_top3_actions(context)
        if top3:
            lines.append("TOP 3 ACTIONS THIS CYCLE")
            lines.append("-" * 40)
            for i, action in enumerate(top3, 1):
                lines.append(f"  {i}. {action}")
            lines.append("")

    # I. System Status (surface trust issues early)
    if context is not None:
        status_issues = build_system_status(context)
        if status_issues:
            lines.append("SYSTEM STATUS")
            lines.append("-" * 40)
            for issue in status_issues:
                lines.append(f"  {issue}")
            lines.append("")

    # Summary
    lines.append("PORTFOLIO SUMMARY")
    lines.append("-" * 40)
    for line in summary_lines:
        lines.append(f"  {line}")
    lines.append("")

    # C. What Changed Since Last Run
    if context is not None:
        changed = build_what_changed(context)
        if changed:
            lines.append("WHAT CHANGED SINCE LAST RUN")
            lines.append("-" * 40)
            for bullet in changed:
                lines.append(f"  {bullet}")
            lines.append("")

    # Action Required (max 3)
    action_items = categorized[ActionLevel.ACTION_REQUIRED][:3]
    if action_items:
        lines.append("🚨 ACTION REQUIRED")
        lines.append("-" * 40)
        for rec in action_items:
            lines.append("")
            lines.append(format_action_required_item(rec).replace("**", ""))
        lines.append("")

    # Recommended (max 5)
    recommended_items = categorized[ActionLevel.RECOMMENDED][:5]
    if recommended_items:
        lines.append("📋 RECOMMENDED")
        lines.append("-" * 40)
        for rec in recommended_items:
            lines.append(format_recommended_item(rec))
        lines.append("")

    # Monitor (max 3)
    monitor_items = categorized[ActionLevel.MONITOR][:3]
    if monitor_items:
        lines.append("👀 MONITOR")
        lines.append("-" * 40)
        for rec in monitor_items:
            lines.append(format_monitor_item(rec))
        lines.append("")

    # J. Theme Engine Highlights
    if context is not None:
        theme_block = build_theme_highlights(context)
        if theme_block:
            lines.append("THEME ENGINE HIGHLIGHTS")
            lines.append("-" * 40)
            for tl in theme_block.splitlines():
                lines.append(f"  {tl}" if tl else "")
            lines.append("")

    # F+G. Behavior Guardrails & Hold Signal
    if context is not None:
        behavior = build_behavior_section(context)
        msgs = behavior.get("messages", [])
        score = behavior.get("do_nothing_score", 0)
        if msgs:
            lines.append("DISCIPLINE CHECK")
            lines.append("-" * 40)
            lines.append(f"  Hold-discipline score: {score}/100")
            for msg in msgs:
                lines.append(f"  {msg}")
            lines.append("")

    # D. Trajectory / Long-Term Outlook
    if context is not None:
        traj = build_trajectory(context)
        if traj:
            lines.append("LONG-TERM TRAJECTORY")
            lines.append("-" * 40)
            lines.append(f"  Expected CAGR:         {traj.get('cagr', 'N/A')}")
            lines.append(f"  5-year projection:     {traj.get('value_5yr', 'N/A')}")
            lines.append(f"  10-year projection:    {traj.get('value_10yr', 'N/A')}")
            lines.append(f"  10yr (growth only):    {traj.get('value_10yr_no_contrib', 'N/A')}")
            lines.append(f"  +$200/mo adds:         {traj.get('extra_200_impact', 'N/A')} over 10yr")
            if "milestone_100k" in traj:
                lines.append("")
                lines.append("  Milestones:")
                lines.append(f"    $100k:  {traj.get('milestone_100k', 'N/A')}")
                lines.append(f"    $250k:  {traj.get('milestone_250k', 'N/A')}")
                lines.append(f"    $500k:  {traj.get('milestone_500k', 'N/A')}")
                lines.append(f"    $1M:    {traj.get('milestone_1m', 'N/A')}")
            if "assumption_note" in traj:
                lines.append(f"  Note: {traj['assumption_note']}")
            lines.append("")

    # E. Opportunity Cost
    if context is not None:
        opp = build_opportunity_cost(context)
        if opp:
            lines.append("OPPORTUNITY COST INSIGHT")
            lines.append("-" * 40)
            lines.append(f"  {opp}")
            lines.append("")

    # H. Holding Rationale
    if context is not None:
        rationale = build_holding_rationale(context)
        if rationale:
            lines.append("POSITION RATIONALE")
            lines.append("-" * 40)
            for sym, reason in rationale.items():
                lines.append(f"  {sym:<6} → {reason}")
            lines.append("")

    # Footer
    lines.append("-" * 52)
    lines.append(f"Generated: {timestamp}")
    lines.append("Automated report. Not financial advice.")

    return "\n".join(lines)


def build_html_body(
    categorized: Dict[ActionLevel, List[FinanceRecommendation]],
    summary_lines: List[str],
    timestamp: str,
    context: Optional['DigestContext'] = None,
) -> str:
    """Build HTML email body, including enhanced sections when context is provided."""
    from digest_builder import (
        build_top3_actions, build_what_changed, build_trajectory,
        build_opportunity_cost, build_behavior_section,
        build_holding_rationale, build_system_status,
        build_theme_highlights,
    )

    # Summary section
    summary_html = "".join(f"<li>{line}</li>" for line in summary_lines)
    
    # Action Required section
    action_html = ""
    action_items = categorized[ActionLevel.ACTION_REQUIRED][:3]
    if action_items:
        items = ""
        for rec in action_items:
            items += f"""
            <div style="margin: 15px 0; padding: 15px; background: #fff5f5; border-left: 4px solid #dc3545; border-radius: 4px;">
                <div style="font-weight: bold; color: #dc3545; margin-bottom: 10px;">{rec.title}</div>
                <div style="font-size: 12px; color: #666; margin-bottom: 8px;">Score: {rec.final_score} | {rec.impact_area.value}</div>
                <ul style="margin: 0; padding-left: 20px; line-height: 1.8;">
                    <li><strong>What:</strong> {rec.what_changed}</li>
                    <li><strong>Why:</strong> {rec.why_it_matters}</li>
                    <li><strong>Do:</strong> {rec.action}</li>
                </ul>
            </div>
            """
        action_html = f"""
        <div style="margin-top: 25px;">
            <h2 style="color: #dc3545; border-bottom: 2px solid #dc3545; padding-bottom: 8px;">
                🚨 Action Required
            </h2>
            {items}
        </div>
        """
    
    # Recommended section
    recommended_html = ""
    recommended_items = categorized[ActionLevel.RECOMMENDED][:5]
    if recommended_items:
        items = "".join(
            f"""<li style="margin: 8px 0; padding: 8px; background: #f8f9fa; border-radius: 4px;">
                <strong>{rec.title}</strong> — {rec.action} — <em>Next: {rec.next_check}</em>
            </li>"""
            for rec in recommended_items
        )
        recommended_html = f"""
        <div style="margin-top: 25px;">
            <h2 style="color: #0d6efd; border-bottom: 2px solid #0d6efd; padding-bottom: 8px;">
                📋 Recommended
            </h2>
            <ul style="list-style: none; padding: 0; margin: 0;">{items}</ul>
        </div>
        """
    
    # Monitor section
    monitor_html = ""
    monitor_items = categorized[ActionLevel.MONITOR][:3]
    if monitor_items:
        items = "".join(
            f"<li style='margin: 5px 0;'>{rec.title}: {rec.what_changed}</li>"
            for rec in monitor_items
        )
        monitor_html = f"""
        <div style="margin-top: 25px;">
            <h2 style="color: #6c757d; border-bottom: 2px solid #6c757d; padding-bottom: 8px;">
                👀 Monitor
            </h2>
            <ul style="padding-left: 20px; color: #666;">{items}</ul>
        </div>
        """
    
    # Count badges
    action_count = len(categorized[ActionLevel.ACTION_REQUIRED])
    rec_count = len(categorized[ActionLevel.RECOMMENDED])

    # ── Enhanced sections (context-driven) ──────────────────────────────
    top3_html = ""
    system_status_html = ""
    what_changed_html = ""
    theme_highlights_html = ""
    behavior_html = ""
    trajectory_html = ""
    opp_cost_html = ""
    rationale_html = ""

    if context is not None:
        # A. Top 3 Actions
        top3 = build_top3_actions(context)
        if top3:
            items_html = "".join(
                f"<li style='margin:8px 0;padding:10px 14px;background:#f0f9ff;"
                f"border-left:3px solid #0d6efd;border-radius:4px;font-size:14px;'>"
                f"<strong>{i}.</strong> {action}</li>"
                for i, action in enumerate(top3, 1)
            )
            top3_html = (
                "<div style='margin-top:20px;padding:16px;background:#e8f4fd;"
                "border-radius:8px;border:1px solid #bee5fc;'>"
                "<h2 style='margin:0 0 12px 0;font-size:15px;color:#0d6efd;'>"
                "&#x1F3AF; Top 3 Actions This Cycle</h2>"
                f"<ol style='list-style:none;padding:0;margin:0;'>{items_html}</ol>"
                "</div>"
            )

        # I. System Status
        issues = build_system_status(context)
        if issues:
            issues_html = "".join(f"<li style='margin:4px 0;'>{iss}</li>" for iss in issues)
            system_status_html = (
                "<div style='margin-top:16px;padding:12px 16px;background:#fff8e1;"
                "border-radius:6px;border-left:3px solid #ffc107;'>"
                "<strong style='font-size:13px;color:#856404;'>&#x26A0; System Status</strong>"
                f"<ul style='margin:6px 0 0 0;padding-left:18px;font-size:13px;color:#666;'>{issues_html}</ul>"
                "</div>"
            )

        # C. What Changed
        changed = build_what_changed(context)
        if changed:
            changed_html = "".join(f"<li style='margin:5px 0;'>{b}</li>" for b in changed)
            what_changed_html = (
                "<div style='margin-top:20px;'>"
                "<h2 style='color:#495057;border-bottom:1px solid #dee2e6;padding-bottom:6px;font-size:14px;'>"
                "&#x1F504; What Changed Since Last Run</h2>"
                f"<ul style='padding-left:20px;color:#555;font-size:14px;'>{changed_html}</ul>"
                "</div>"
            )

        # J. Theme Engine Highlights
        theme_block = build_theme_highlights(context)
        if theme_block:
            theme_lines_html = "".join(
                f"<li style='margin:5px 0;font-size:13px;color:#555;'>{line}</li>"
                for line in theme_block.splitlines()
                if line.strip().startswith("•")
            )
            theme_sections: list[str] = []
            for line in theme_block.splitlines():
                if line and not line.startswith(" ") and not line.startswith("•"):
                    theme_sections.append(
                        f"<p style='margin:10px 0 4px 0;font-size:13px;"
                        f"font-weight:bold;color:#0d6efd;'>{line}</p>"
                    )
                elif line.strip().startswith("•"):
                    theme_sections.append(
                        f"<p style='margin:3px 0 3px 12px;font-size:13px;color:#555;'>{line.strip()}</p>"
                    )
            theme_highlights_html = (
                "<div style='margin-top:20px;padding:14px;background:#f0f8ff;"
                "border-radius:6px;border-left:3px solid #0d6efd;'>"
                "<h2 style='margin:0 0 10px 0;font-size:14px;color:#0d6efd;'>"
                "&#x1F52D; Theme Engine Highlights</h2>"
                + "".join(theme_sections)
                + "</div>"
            )
        else:
            theme_highlights_html = ""

        # F+G. Behavior Guardrails
        behavior = build_behavior_section(context)
        msgs = behavior.get("messages", [])
        score = behavior.get("do_nothing_score", 0)
        hold = behavior.get("hold_signal", False)
        if msgs:
            badge_color = "#198754" if hold else "#fd7e14"
            badge_text = "HOLD" if hold else "ACT"
            msgs_html = "".join(f"<li style='margin:5px 0;'>{m}</li>" for m in msgs)
            behavior_html = (
                "<div style='margin-top:20px;padding:14px;background:#f8f9fa;"
                "border-radius:6px;'>"
                "<h2 style='margin:0 0 10px 0;font-size:14px;color:#495057;'>"
                "&#x1F4AA; Discipline Check &nbsp;"
                f"<span style='background:{badge_color};color:white;padding:2px 10px;"
                f"border-radius:12px;font-size:12px;'>{badge_text} — {score}/100</span></h2>"
                f"<ul style='padding-left:18px;margin:0;font-size:13px;color:#555;'>{msgs_html}</ul>"
                "</div>"
            )

        # D. Trajectory
        traj = build_trajectory(context)
        if traj:
            proj_rows = (
                f"<tr><td style='padding:4px 8px;color:#888;'>Expected CAGR</td>"
                f"<td style='padding:4px 8px;font-weight:bold;'>{traj.get('cagr','N/A')}</td></tr>"
                f"<tr><td style='padding:4px 8px;color:#888;'>5-year value</td>"
                f"<td style='padding:4px 8px;font-weight:bold;'>{traj.get('value_5yr','N/A')}</td></tr>"
                f"<tr><td style='padding:4px 8px;color:#888;'>10-year value</td>"
                f"<td style='padding:4px 8px;font-weight:bold;'>{traj.get('value_10yr','N/A')}</td></tr>"
                f"<tr><td style='padding:4px 8px;color:#888;'>10yr (no contrib)</td>"
                f"<td style='padding:4px 8px;'>{traj.get('value_10yr_no_contrib','N/A')}</td></tr>"
                f"<tr><td style='padding:4px 8px;color:#888;'>+$200/mo impact</td>"
                f"<td style='padding:4px 8px;color:#198754;'>{traj.get('extra_200_impact','N/A')}</td></tr>"
            )
            milestone_rows = ""
            if "milestone_100k" in traj:
                milestone_rows = (
                    f"<tr><td style='padding:4px 8px;color:#888;'>To $100k</td>"
                    f"<td style='padding:4px 8px;'>{traj.get('milestone_100k','N/A')}</td></tr>"
                    f"<tr><td style='padding:4px 8px;color:#888;'>To $250k</td>"
                    f"<td style='padding:4px 8px;'>{traj.get('milestone_250k','N/A')}</td></tr>"
                    f"<tr><td style='padding:4px 8px;color:#888;'>To $500k</td>"
                    f"<td style='padding:4px 8px;'>{traj.get('milestone_500k','N/A')}</td></tr>"
                    f"<tr><td style='padding:4px 8px;color:#888;'>To $1M</td>"
                    f"<td style='padding:4px 8px;'>{traj.get('milestone_1m','N/A')}</td></tr>"
                )
            note = traj.get("assumption_note", "")
            trajectory_html = (
                "<div style='margin-top:20px;'>"
                "<h2 style='color:#6f42c1;border-bottom:2px solid #6f42c1;padding-bottom:8px;font-size:14px;'>"
                "&#x1F4C8; Long-Term Trajectory</h2>"
                "<table style='width:100%;font-size:13px;border-collapse:collapse;'>"
                f"<tbody>{proj_rows}{milestone_rows}</tbody></table>"
                f"<p style='font-size:11px;color:#999;margin-top:8px;'>{note}</p>"
                "</div>"
            )

        # E. Opportunity Cost
        opp = build_opportunity_cost(context)
        if opp:
            opp_cost_html = (
                "<div style='margin-top:16px;padding:12px 16px;background:#fff3cd;"
                "border-radius:6px;border-left:3px solid #ffc107;font-size:13px;color:#856404;'>"
                "<strong>&#x1F4B0; Opportunity Cost Insight</strong><br>"
                f"<span style='color:#666;'>{opp}</span>"
                "</div>"
            )

        # H. Position Rationale
        rationale = build_holding_rationale(context)
        if rationale:
            rat_rows = "".join(
                f"<tr><td style='padding:5px 10px;font-weight:bold;'>{sym}</td>"
                f"<td style='padding:5px 10px;color:#555;font-size:13px;'>{reason}</td></tr>"
                for sym, reason in rationale.items()
            )
            rationale_html = (
                "<div style='margin-top:20px;'>"
                "<h2 style='color:#495057;border-bottom:1px solid #dee2e6;"
                "padding-bottom:6px;font-size:14px;'>&#x1F4CB; Why Each Position Exists</h2>"
                f"<table style='width:100%;border-collapse:collapse;'><tbody>{rat_rows}</tbody></table>"
                "</div>"
            )

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">

        <!-- Header -->
        <div style="text-align: center; padding: 20px; background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); color: white; border-radius: 10px;">
            <h1 style="margin: 0; font-size: 22px;">&#x1F4CA; Portfolio Digest</h1>
            <p style="margin: 10px 0 0 0; opacity: 0.8; font-size: 13px;">{timestamp}</p>
            <div style="margin-top: 12px;">
                {f'<span style="background: #dc3545; padding: 4px 12px; border-radius: 20px; font-size: 12px; margin: 0 4px;">{action_count} Action Required</span>' if action_count else ''}
                {f'<span style="background: #0d6efd; padding: 4px 12px; border-radius: 20px; font-size: 12px; margin: 0 4px;">{rec_count} Recommended</span>' if rec_count else ''}
            </div>
        </div>

        {top3_html}
        {system_status_html}

        <!-- Summary -->
        <div style="margin-top: 20px; padding: 16px; background: #f8f9fa; border-radius: 8px;">
            <h2 style="margin: 0 0 12px 0; font-size: 15px; color: #495057;">Portfolio Summary</h2>
            <ul style="margin: 0; padding-left: 20px; line-height: 2; font-size: 14px;">
                {summary_html}
            </ul>
        </div>

        {what_changed_html}
        {action_html}
        {recommended_html}
        {monitor_html}
        {theme_highlights_html}
        {behavior_html}
        {trajectory_html}
        {opp_cost_html}
        {rationale_html}

        <!-- Footer -->
        <div style="margin-top: 30px; padding-top: 16px; border-top: 1px solid #dee2e6; text-align: center; font-size: 11px; color: #6c757d;">
            <p style="margin: 0;">Automated report. Not financial advice.</p>
            <p style="margin: 4px 0 0 0;">Portfolio Automation System</p>
        </div>

    </body>
    </html>
    """

    return html


def _build_monthly_memo_text(
    summary_lines: List[str],
    contribution_rows: List[Dict],
    dashboard_dict: Dict,
    drawdown_regime: str,
    timestamp: str,
    context: Optional['DigestContext'] = None,
) -> str:
    """Build plain-text body for the monthly Capital Deployment Memo."""
    from digest_builder import (
        build_top3_actions, build_what_changed, build_behavior_section,
    )

    lines = [
        "=" * 62,
        "  MONTHLY CAPITAL DEPLOYMENT MEMO",
        "=" * 62,
        "",
    ]

    # A. Top 3 Actions
    if context is not None:
        top3 = build_top3_actions(context)
        if top3:
            lines += ["TOP 3 ACTIONS THIS MONTH", "-" * 40]
            for i, action in enumerate(top3, 1):
                lines.append(f"  {i}. {action}")
            lines.append("")

    lines += [
        "PORTFOLIO STATUS",
        "-" * 40,
    ]
    for line in summary_lines:
        lines.append(f"  {line}")
    lines.append(f"  Drawdown regime: {drawdown_regime}")
    lines.append("")

    # C. What Changed
    if context is not None:
        changed = build_what_changed(context)
        if changed:
            lines += ["WHAT CHANGED SINCE LAST MONTH", "-" * 40]
            for bullet in changed:
                lines.append(f"  {bullet}")
            lines.append("")

    lines += [
        "PROJECTIONS",
        "-" * 40,
        f"  Expected CAGR:          {dashboard_dict.get('ExpectedCAGR', 'N/A')}",
        f"  5-year value:           (see dashboard for 5yr)",
        f"  10-year with contrib:   {format_currency(float(dashboard_dict.get('Projected10yr', 0)))}",
        f"  10-year growth only:    {format_currency(float(dashboard_dict.get('Projected10yrNoContrib', 0)))}",
        f"  Impact of +$200/mo:     +{format_currency(float(dashboard_dict.get('Extra200Impact', 0)))}",
        "",
        "MILESTONES",
        "-" * 40,
        f"  To $100k:  {dashboard_dict.get('YearsTo100k', 'N/A')}",
        f"  To $250k:  {dashboard_dict.get('YearsTo250k', 'N/A')}",
        f"  To $500k:  {dashboard_dict.get('YearsTo500k', 'N/A')}",
        f"  To $1M:    {dashboard_dict.get('YearsTo1m', 'N/A')}",
        "",
    ]

    if contribution_rows:
        lines += ["THIS MONTH'S CONTRIBUTION PLAN", "-" * 40]
        total = 0.0
        for row in contribution_rows:
            symbol = row.get('Symbol', '')
            dollars = row.get('RecommendedContributionDollars', 0)
            drift = row.get('Drift', '')
            reason = row.get('Reason', '')
            if symbol and dollars:
                lines.append(
                    f"  {symbol:<6}  ${float(dollars):>8,.2f}  {drift} drift  {reason}"
                )
                total += float(dollars)
        lines.append(f"  {'TOTAL':<6}  ${total:>8,.2f}")
        lines.append("")

    # J. Strategic Takeaway (CEO summary)
    if context is not None:
        behavior = build_behavior_section(context)
        score = behavior.get("do_nothing_score", 0)
        msgs = behavior.get("messages", [])
        lines += ["STRATEGIC TAKEAWAY", "-" * 40]
        _regime_notes = {
            "normal":           "Portfolio is in healthy territory.",
            "modest_dip":       "Minor drawdown underway — contributions tilted to equity.",
            "significant_dip":  "Meaningful drawdown — anti-panic mode active, buy the dip.",
            "severe_dip":       "Severe drawdown — deploy all available cash to equity.",
        }
        lines.append(f"  Regime: {_regime_notes.get(drawdown_regime, drawdown_regime)}")
        lines.append(f"  Discipline score: {score}/100")
        if msgs:
            lines.append(f"  {msgs[0]}")
        lines.append(f"  Next focus: execute this month's contribution plan.")
        lines.append("")

    lines += [
        "-" * 40,
        f"Generated: {timestamp}",
        "Config-driven assumptions only. Not financial advice.",
    ]
    return "\n".join(lines)


def _build_monthly_memo_html(
    summary_lines: List[str],
    contribution_rows: List[Dict],
    dashboard_dict: Dict,
    drawdown_regime: str,
    timestamp: str,
    context: Optional['DigestContext'] = None,
) -> str:
    """Build HTML body for the monthly Capital Deployment Memo."""
    from digest_builder import (
        build_top3_actions, build_what_changed, build_behavior_section,
        build_trajectory,
    )

    summary_html = "".join(f"<li>{line}</li>" for line in summary_lines)
    summary_html += f"<li>Drawdown regime: <strong>{drawdown_regime}</strong></li>"

    proj_10yr = format_currency(float(dashboard_dict.get('Projected10yr', 0)))
    proj_no_c = format_currency(float(dashboard_dict.get('Projected10yrNoContrib', 0)))
    extra_200 = format_currency(float(dashboard_dict.get('Extra200Impact', 0)))

    projection_html = (
        f"<li>Expected CAGR: <strong>{dashboard_dict.get('ExpectedCAGR', 'N/A')}</strong></li>"
        f"<li>With contributions: <strong>{proj_10yr}</strong></li>"
        f"<li>Growth only: <strong>{proj_no_c}</strong></li>"
        f"<li>Impact of +$200/mo: <strong>+{extra_200}</strong></li>"
    )

    milestone_html = (
        f"<li>$100k: {dashboard_dict.get('YearsTo100k', 'N/A')}</li>"
        f"<li>$250k: {dashboard_dict.get('YearsTo250k', 'N/A')}</li>"
        f"<li>$500k: {dashboard_dict.get('YearsTo500k', 'N/A')}</li>"
        f"<li>$1M:   {dashboard_dict.get('YearsTo1m', 'N/A')}</li>"
    )

    contrib_html = ""
    if contribution_rows:
        rows_html = ""
        total = 0.0
        for row in contribution_rows:
            symbol = row.get('Symbol', '')
            dollars = row.get('RecommendedContributionDollars', 0)
            drift = row.get('Drift', '')
            reason = row.get('Reason', '')
            if symbol and dollars:
                rows_html += (
                    f"<tr>"
                    f"<td style='padding:6px 10px;font-weight:bold;'>{symbol}</td>"
                    f"<td style='padding:6px 10px;text-align:right;'>${float(dollars):,.2f}</td>"
                    f"<td style='padding:6px 10px;color:#666;'>{drift}</td>"
                    f"<td style='padding:6px 10px;font-size:12px;color:#888;'>{reason}</td>"
                    f"</tr>"
                )
                total += float(dollars)
        rows_html += (
            "<tr style='border-top:2px solid #333;font-weight:bold;'>"
            "<td style='padding:6px 10px;'>TOTAL</td>"
            f"<td style='padding:6px 10px;text-align:right;'>${total:,.2f}</td>"
            "<td colspan='2'></td></tr>"
        )
        contrib_html = (
            "<div style='margin-top:25px;'>"
            "<h2 style='color:#198754;border-bottom:2px solid #198754;padding-bottom:8px;'>"
            "&#x1F4B0; This Month's Contribution Plan</h2>"
            "<table style='width:100%;border-collapse:collapse;font-size:14px;'>"
            "<thead><tr style='background:#f8f9fa;'>"
            "<th style='padding:6px 10px;text-align:left;'>Symbol</th>"
            "<th style='padding:6px 10px;text-align:right;'>Amount</th>"
            "<th style='padding:6px 10px;text-align:left;'>Drift</th>"
            "<th style='padding:6px 10px;text-align:left;'>Reason</th>"
            f"</tr></thead><tbody>{rows_html}</tbody></table></div>"
        )

    # ── Enhanced sections ────────────────────────────────────────────────
    top3_memo_html = ""
    what_changed_memo_html = ""
    strategic_html = ""

    if context is not None:
        # A. Top 3 Actions
        top3 = build_top3_actions(context)
        if top3:
            items_html = "".join(
                f"<li style='margin:6px 0;padding:8px 12px;background:#f0f9ff;"
                f"border-left:3px solid #0d6efd;border-radius:4px;'>"
                f"<strong>{i}.</strong> {a}</li>"
                for i, a in enumerate(top3, 1)
            )
            top3_memo_html = (
                "<div style='margin-top:16px;padding:14px;background:#e8f4fd;"
                "border-radius:8px;'>"
                "<h2 style='margin:0 0 10px 0;font-size:14px;color:#0d6efd;'>"
                "&#x1F3AF; Top 3 Actions This Month</h2>"
                f"<ol style='list-style:none;padding:0;margin:0;'>{items_html}</ol>"
                "</div>"
            )

        # C. What Changed
        changed = build_what_changed(context)
        if changed:
            ch_html = "".join(f"<li style='margin:4px 0;'>{b}</li>" for b in changed)
            what_changed_memo_html = (
                "<div style='margin-top:16px;'>"
                "<h2 style='font-size:14px;color:#495057;border-bottom:1px solid #dee2e6;"
                "padding-bottom:6px;'>&#x1F504; What Changed</h2>"
                f"<ul style='padding-left:18px;font-size:13px;color:#555;'>{ch_html}</ul>"
                "</div>"
            )

        # J. Strategic Takeaway
        behavior = build_behavior_section(context)
        score = behavior.get("do_nothing_score", 0)
        msgs = behavior.get("messages", [])
        regime_notes = {
            "normal":           "Portfolio is in healthy territory. Stay the course.",
            "modest_dip":       "Minor drawdown — contributions tilted toward equity.",
            "significant_dip":  "Significant drawdown — anti-panic mode active. Buy the dip.",
            "severe_dip":       "Severe drawdown — deploy all available cash to equity.",
        }
        regime_note = regime_notes.get(drawdown_regime, drawdown_regime)
        first_msg = msgs[0] if msgs else ""
        strategic_html = (
            "<div style='margin-top:20px;padding:14px;background:#f8f9fa;"
            "border-radius:8px;border-left:3px solid #198754;'>"
            "<h2 style='margin:0 0 8px 0;font-size:14px;color:#198754;'>&#x1F3AF; Strategic Takeaway</h2>"
            f"<p style='margin:0 0 6px 0;font-size:13px;'><strong>Regime:</strong> {regime_note}</p>"
            f"<p style='margin:0 0 6px 0;font-size:13px;'><strong>Discipline score:</strong> {score}/100</p>"
            f"<p style='margin:0 0 6px 0;font-size:13px;'>{first_msg}</p>"
            "<p style='margin:0;font-size:13px;color:#555;'>"
            "<strong>Next focus:</strong> execute this month's contribution plan consistently.</p>"
            "</div>"
        )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;line-height:1.6;color:#333;max-width:600px;margin:0 auto;padding:20px;">
  <div style="text-align:center;padding:20px;background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);color:white;border-radius:10px;">
    <h1 style="margin:0;font-size:22px;">&#x1F4C5; Monthly Capital Deployment Memo</h1>
    <p style="margin:8px 0 0 0;opacity:0.8;font-size:13px;">{timestamp}</p>
  </div>
  {top3_memo_html}
  <div style="margin-top:16px;padding:15px;background:#f8f9fa;border-radius:8px;">
    <h2 style="margin:0 0 10px 0;font-size:15px;color:#495057;">Portfolio Status</h2>
    <ul style="margin:0;padding-left:20px;line-height:2;">{summary_html}</ul>
  </div>
  {what_changed_memo_html}
  <div style="margin-top:16px;">
    <h2 style="color:#0d6efd;border-bottom:2px solid #0d6efd;padding-bottom:8px;">&#x1F4C8; Projections</h2>
    <ul style="line-height:2;">{projection_html}</ul>
  </div>
  <div style="margin-top:16px;">
    <h2 style="color:#6f42c1;border-bottom:2px solid #6f42c1;padding-bottom:8px;">&#x1F3C1; Milestones</h2>
    <ul style="line-height:2;">{milestone_html}</ul>
  </div>
  {contrib_html}
  {strategic_html}
  <div style="margin-top:20px;padding-top:12px;border-top:1px solid #dee2e6;text-align:center;font-size:11px;color:#6c757d;">
    <p style="margin:0;">Config-driven assumptions only. Not financial advice.</p>
    <p style="margin:4px 0 0 0;">Portfolio Automation System</p>
  </div>
</body>
</html>"""


class FinanceEmailDigest:
    """Email digest sender for finance recommendations."""
    
    def __init__(
        self,
        smtp_server: str = "smtp.gmail.com",
        smtp_port: int = 587,
        use_tls: bool = True,
        sender_email: Optional[str] = None,
        recipient_email: Optional[str] = None,
        password: Optional[str] = None
    ):
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.use_tls = use_tls
        self.sender_email = sender_email or get_env('EMAIL_SENDER')
        self.recipient_email = recipient_email or get_env('EMAIL_RECIPIENT')
        self.password = password or get_env('EMAIL_PASSWORD')
    
    def is_configured(self) -> bool:
        """Check if email is properly configured."""
        return all([self.sender_email, self.recipient_email, self.password])
    
    def send_digest(
        self,
        recommendations: List[FinanceRecommendation],
        summary_lines: List[str],
        is_digest_day: bool = False,
        force_send: bool = False,
        context: Optional['DigestContext'] = None,
    ) -> bool:
        """
        Send finance digest email.

        Args:
            context: Optional DigestContext for enhanced sections (Top 3 Actions,
                     What Changed, Trajectory, etc.).  Pass None to send the
                     classic digest without enhanced sections.

        Returns True if email was sent successfully.
        """
        if not self.is_configured():
            logger.warning("Email not configured, skipping send")
            return False

        # Deduplicate and filter
        deduped = deduplicate_recommendations(recommendations)
        filtered = filter_for_email(deduped)

        # Check if we should send
        if not force_send and not should_send_email(filtered, is_digest_day):
            logger.info("No email needed based on current recommendations")
            return False

        # Categorize
        categorized = categorize_recommendations(filtered)

        # Build email
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
        subject = build_email_subject(categorized)
        text_body = build_text_body(categorized, summary_lines, timestamp, context=context)
        html_body = build_html_body(categorized, summary_lines, timestamp, context=context)
        
        # Send
        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = self.sender_email
            msg['To'] = self.recipient_email
            
            msg.attach(MIMEText(text_body, 'plain', 'utf-8'))
            msg.attach(MIMEText(html_body, 'html', 'utf-8'))
            
            context = ssl.create_default_context()
            
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                if self.use_tls:
                    server.starttls(context=context)
                server.login(self.sender_email, self.password)
                server.sendmail(self.sender_email, self.recipient_email, msg.as_string())
            
            logger.info(f"Finance digest sent to {self.recipient_email}")
            
            # Mark recommendations as sent
            for rec in filtered:
                rec.last_sent = datetime.now()
            
            return True
            
        except smtplib.SMTPException as e:
            logger.error(f"Failed to send digest: {e}")
            raise EmailDigestError(f"SMTP error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error sending digest: {e}")
            raise EmailDigestError(f"Unexpected error: {e}")

    def send_monthly_memo(
        self,
        summary_lines: List[str],
        contribution_rows: List[Dict],
        dashboard_dict: Dict,
        drawdown_regime: str = 'normal',
        context: Optional['DigestContext'] = None,
    ) -> bool:
        """
        Send the monthly Capital Deployment Memo.

        Always sent when run_mode='monthly'. Includes portfolio status,
        10-year projections, milestones, contribution plan, and (when context
        is provided) Top 3 Actions, What Changed, and Strategic Takeaway.

        Args:
            summary_lines:     Top-level portfolio summary lines.
            contribution_rows: List of dicts from ContributionAllocation.to_dict().
            dashboard_dict:    Dict from CompoundingDashboard.to_dict().
            drawdown_regime:   Current drawdown regime label.
            context:           Optional DigestContext for enhanced sections.

        Returns:
            True if the email was sent successfully.
        """
        if not self.is_configured():
            logger.warning("Email not configured, skipping monthly memo")
            return False

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
        subject = f"Monthly Capital Deployment Memo — {date.today().strftime('%B %Y')}"
        text_body = _build_monthly_memo_text(
            summary_lines, contribution_rows, dashboard_dict, drawdown_regime, timestamp,
            context=context,
        )
        html_body = _build_monthly_memo_html(
            summary_lines, contribution_rows, dashboard_dict, drawdown_regime, timestamp,
            context=context,
        )

        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = self.sender_email
            msg['To'] = self.recipient_email

            msg.attach(MIMEText(text_body, 'plain', 'utf-8'))
            msg.attach(MIMEText(html_body, 'html', 'utf-8'))

            context = ssl.create_default_context()

            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                if self.use_tls:
                    server.starttls(context=context)
                server.login(self.sender_email, self.password)
                server.sendmail(self.sender_email, self.recipient_email, msg.as_string())

            logger.info(f"Monthly capital memo sent to {self.recipient_email}")
            return True

        except smtplib.SMTPException as e:
            logger.error(f"Failed to send monthly memo: {e}")
            raise EmailDigestError(f"SMTP error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error sending monthly memo: {e}")
            raise EmailDigestError(f"Unexpected error: {e}")


def format_recommendations_for_csv(
    recommendations: List[FinanceRecommendation]
) -> List[Dict]:
    """Format recommendations for CSV export."""
    rows = []
    for rec in recommendations:
        rows.append({
            'ID': rec.id,
            'Score': rec.final_score,
            'Level': rec.action_level.value,
            'Area': rec.impact_area.value,
            'Title': rec.title,
            'Trigger': rec.trigger,
            'What_Changed': rec.what_changed,
            'Why': rec.why_it_matters,
            'Action': rec.action,
            'Next_Check': rec.next_check,
            'Evidence': rec.evidence,
            'Severity': rec.components.severity,
            'Persistence': rec.components.persistence,
            'Impact': rec.components.impact,
            'Priority': rec.components.priority,
            'Confidence': rec.components.confidence,
            'Created': rec.created_at.isoformat()
        })
    return rows


# ---------------------------------------------------------------------------
# Digest hashing — used by main.py for email deduplication via state_store
# ---------------------------------------------------------------------------

def compute_digest_hash(
    recommendations: List[FinanceRecommendation],
    summary_lines: List[str],
) -> str:
    """
    Compute a stable SHA-256 hash of a weekly/daily digest email's content.

    Used by main.py to check email_history before sending and to record a
    sent digest, preventing duplicate emails within a 7-day window.

    The hash covers recommendation IDs, scores, action levels, and summary
    lines.  Timestamps are excluded so two runs with the same underlying
    data produce the same hash.

    Args:
        recommendations: List of FinanceRecommendation objects.
        summary_lines:   Top-level portfolio summary strings.

    Returns:
        64-character lowercase hex SHA-256 digest.
    """
    payload = {
        'summary': summary_lines,
        'recs': [
            {
                'id': r.id,
                'score': r.final_score,
                'level': r.action_level.value,
            }
            for r in sorted(recommendations, key=lambda r: r.id)
        ],
    }
    raw = _json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def compute_monthly_memo_hash(
    summary_lines: List[str],
    contrib_rows: List[Dict],
    dashboard_dict: Dict,
) -> str:
    """
    Compute a stable SHA-256 hash of a monthly Capital Deployment Memo.

    Covers summary lines, contribution allocation symbols + amounts, and
    key dashboard projection values.  Generated timestamps are excluded.

    Args:
        summary_lines:  Top-level portfolio summary strings.
        contrib_rows:   List of dicts from ContributionAllocation.to_dict().
        dashboard_dict: Dict from CompoundingDashboard.to_dict().

    Returns:
        64-character lowercase hex SHA-256 digest.
    """
    stable_dashboard = {
        k: dashboard_dict.get(k)
        for k in (
            'ExpectedCAGR', 'Projected10yr', 'Projected10yrNoContrib',
            'YearsTo100k', 'YearsTo250k', 'YearsTo500k', 'YearsTo1m',
        )
    }
    payload = {
        'summary': summary_lines,
        'contrib': [
            {
                'symbol': r.get('Symbol'),
                'dollars': r.get('RecommendedContributionDollars'),
            }
            for r in sorted(contrib_rows, key=lambda r: r.get('Symbol', ''))
        ],
        'dashboard': stable_dashboard,
    }
    raw = _json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()
