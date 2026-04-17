"""
Email reporting module.
Sends portfolio summary reports via SMTP (Gmail compatible).
"""

import logging
import smtplib
import ssl
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from utils import (
    get_env, format_currency, format_percent,
    is_weekly_summary_day, is_annual_review_date
)
from portfolio import PortfolioSummary
from recommendations import RecommendationReport, ActionType, get_action_summary


logger = logging.getLogger('portfolio_automation.email')


class EmailError(Exception):
    """Custom exception for email errors."""
    pass


class EmailReporter:
    """SMTP email reporter for portfolio updates."""
    
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
        
        # Allow env overrides
        self.sender_email = sender_email or get_env('EMAIL_SENDER')
        self.recipient_email = recipient_email or get_env('EMAIL_RECIPIENT')
        self.password = password or get_env('EMAIL_PASSWORD')
    
    def is_configured(self) -> bool:
        """Check if email is properly configured."""
        return all([
            self.sender_email,
            self.recipient_email,
            self.password
        ])
    
    def send_report(
        self,
        summary: PortfolioSummary,
        recommendations: RecommendationReport,
        is_weekly: bool = False,
        is_annual: bool = False,
        additional_notes: Optional[list[str]] = None
    ) -> bool:
        """
        Send portfolio report email.
        Returns True if successful.
        """
        if not self.is_configured():
            logger.warning("Email not configured, skipping send")
            return False
        
        try:
            # Build email
            msg = MIMEMultipart('alternative')
            msg['Subject'] = self._build_subject(summary, is_weekly, is_annual)
            msg['From'] = self.sender_email
            msg['To'] = self.recipient_email
            
            # Generate content
            text_body = self._build_text_body(
                summary, recommendations, is_weekly, is_annual, additional_notes
            )
            html_body = self._build_html_body(
                summary, recommendations, is_weekly, is_annual, additional_notes
            )
            
            msg.attach(MIMEText(text_body, 'plain'))
            msg.attach(MIMEText(html_body, 'html'))
            
            # Send email
            context = ssl.create_default_context()
            
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                if self.use_tls:
                    server.starttls(context=context)
                
                server.login(self.sender_email, self.password)
                server.sendmail(
                    self.sender_email,
                    self.recipient_email,
                    msg.as_string()
                )
            
            logger.info(f"Email report sent to {self.recipient_email}")
            return True
            
        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"Email authentication failed: {e}")
            raise EmailError(f"Authentication failed: {e}")
            
        except smtplib.SMTPException as e:
            logger.error(f"Email send failed: {e}")
            raise EmailError(f"Send failed: {e}")
            
        except Exception as e:
            logger.error(f"Unexpected email error: {e}")
            raise EmailError(f"Unexpected error: {e}")
    
    def _build_subject(
        self,
        summary: PortfolioSummary,
        is_weekly: bool,
        is_annual: bool
    ) -> str:
        """Build email subject line."""
        today = date.today().strftime('%Y-%m-%d')
        
        if is_annual:
            return f"📅 Annual Portfolio Review — {today}"
        elif is_weekly:
            return f"📊 Weekly Portfolio Summary — {today}"
        elif summary.has_breach:
            return f"⚠️ Portfolio Update — Rebalance Alert — {today}"
        else:
            return f"Portfolio Update — {today}"
    
    def _build_text_body(
        self,
        summary: PortfolioSummary,
        recommendations: RecommendationReport,
        is_weekly: bool,
        is_annual: bool,
        additional_notes: Optional[list[str]]
    ) -> str:
        """Build plain text email body."""
        lines = []
        
        # Header
        if is_annual:
            lines.append("ANNUAL PORTFOLIO REVIEW")
            lines.append("=" * 40)
            lines.append("")
        elif is_weekly:
            lines.append("WEEKLY PORTFOLIO SUMMARY")
            lines.append("=" * 40)
            lines.append("")
        
        # Values
        lines.append("PORTFOLIO VALUES")
        lines.append("-" * 40)
        lines.append(f"Total Portfolio:  {format_currency(summary.total_portfolio_value)}")
        lines.append(f"Cash Available:   {format_currency(summary.cash_value)}")
        
        if summary.retirement_401k_value > 0:
            lines.append(f"401(k) Balance:   {format_currency(summary.retirement_401k_value)}")
            lines.append(f"Total Net Worth:  {format_currency(summary.total_net_worth)}")
        
        lines.append("")
        
        # Drift
        lines.append("ALLOCATION STATUS")
        lines.append("-" * 40)
        lines.append(f"Max Drift:        {format_percent(summary.max_drift)} ({summary.max_drift_symbol})")
        lines.append(f"Band Threshold:   ±{format_percent(summary.breach_threshold)}")
        
        if summary.has_breach:
            lines.append("")
            lines.append("⚠️  REBALANCE RECOMMENDED")
        else:
            lines.append("")
            lines.append("✓ Portfolio within target bands")
        
        lines.append("")
        
        # Recommendations
        lines.append("RECOMMENDED ACTIONS")
        lines.append("-" * 40)
        lines.append(recommendations.summary_message)
        lines.append("")
        
        action_summary = get_action_summary(recommendations)
        
        if action_summary['buy_count'] > 0:
            lines.append(f"BUY: {action_summary['buy_count']} action(s), "
                        f"~{format_currency(action_summary['total_buy_amount'])}")
        
        if action_summary['sell_count'] > 0:
            lines.append(f"SELL: {action_summary['sell_count']} action(s), "
                        f"~{format_currency(action_summary['total_sell_amount'])}")
        
        if action_summary['hold_count'] > 0:
            lines.append(f"HOLD: {action_summary['hold_count']} position(s)")
        
        # Action details
        for rec in recommendations.recommendations:
            if rec.action_type in [ActionType.BUY, ActionType.SELL, ActionType.REBALANCE_ALERT]:
                lines.append("")
                lines.append(f"  {rec.action_type.value}: {rec.symbol}")
                if rec.shares and rec.amount:
                    lines.append(f"    {rec.shares:.0f} shares (~{format_currency(rec.amount)})")
                lines.append(f"    {rec.reason}")
        
        lines.append("")
        
        # Notes
        all_notes = list(recommendations.notes)
        if additional_notes:
            all_notes.extend(additional_notes)
        
        if is_annual:
            all_notes.append("Annual review: Consider rebalancing and tax-loss harvesting")
        
        if all_notes:
            lines.append("NOTES")
            lines.append("-" * 40)
            for note in all_notes:
                lines.append(f"• {note}")
            lines.append("")
        
        # Footer
        lines.append("-" * 40)
        lines.append(f"Generated: {summary.timestamp}")
        lines.append("This is an automated report. Not financial advice.")
        
        return "\n".join(lines)
    
    def _build_html_body(
        self,
        summary: PortfolioSummary,
        recommendations: RecommendationReport,
        is_weekly: bool,
        is_annual: bool,
        additional_notes: Optional[list[str]]
    ) -> str:
        """Build HTML email body."""
        status_color = "#dc3545" if summary.has_breach else "#28a745"
        status_text = "Rebalance Recommended" if summary.has_breach else "Within Target"
        
        all_notes = list(recommendations.notes)
        if additional_notes:
            all_notes.extend(additional_notes)
        if is_annual:
            all_notes.append("Annual review: Consider rebalancing and tax-loss harvesting")
        
        notes_html = ""
        if all_notes:
            notes_items = "".join(f"<li>{note}</li>" for note in all_notes)
            notes_html = f"""
            <div style="margin-top: 20px; padding: 15px; background-color: #f8f9fa; border-radius: 5px;">
                <h3 style="margin-top: 0; color: #495057;">Notes</h3>
                <ul style="margin: 0; padding-left: 20px;">{notes_items}</ul>
            </div>
            """
        
        # Build recommendations section
        recs_html = ""
        for rec in recommendations.recommendations:
            if rec.action_type in [ActionType.BUY, ActionType.SELL, ActionType.REBALANCE_ALERT]:
                badge_color = {
                    ActionType.BUY: "#28a745",
                    ActionType.SELL: "#dc3545",
                    ActionType.REBALANCE_ALERT: "#ffc107"
                }.get(rec.action_type, "#6c757d")
                
                amount_text = ""
                if rec.shares and rec.amount:
                    amount_text = f" — {rec.shares:.0f} shares (~{format_currency(rec.amount)})"
                
                recs_html += f"""
                <div style="margin: 10px 0; padding: 10px; border-left: 4px solid {badge_color}; background-color: #f8f9fa;">
                    <strong style="color: {badge_color};">{rec.action_type.value}</strong>: {rec.symbol}{amount_text}
                    <br><small style="color: #6c757d;">{rec.reason}</small>
                </div>
                """
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ text-align: center; padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border-radius: 10px 10px 0 0; }}
                .content {{ background: white; padding: 20px; border: 1px solid #ddd; }}
                .metric {{ display: inline-block; padding: 15px; margin: 5px; background: #f8f9fa; border-radius: 5px; min-width: 120px; text-align: center; }}
                .metric-value {{ font-size: 24px; font-weight: bold; color: #333; }}
                .metric-label {{ font-size: 12px; color: #6c757d; text-transform: uppercase; }}
                .status-badge {{ display: inline-block; padding: 5px 15px; border-radius: 20px; font-weight: bold; }}
                .footer {{ text-align: center; padding: 15px; font-size: 12px; color: #6c757d; background: #f8f9fa; border-radius: 0 0 10px 10px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1 style="margin: 0;">{'📅 Annual Review' if is_annual else '📊 Weekly Summary' if is_weekly else '📈 Portfolio Update'}</h1>
                    <p style="margin: 10px 0 0 0; opacity: 0.9;">{summary.timestamp}</p>
                </div>
                
                <div class="content">
                    <div style="text-align: center; margin-bottom: 20px;">
                        <div class="metric">
                            <div class="metric-value">{format_currency(summary.total_portfolio_value)}</div>
                            <div class="metric-label">Total Portfolio</div>
                        </div>
                        <div class="metric">
                            <div class="metric-value">{format_currency(summary.cash_value)}</div>
                            <div class="metric-label">Cash Available</div>
                        </div>
                        {f'''
                        <div class="metric">
                            <div class="metric-value">{format_currency(summary.total_net_worth)}</div>
                            <div class="metric-label">Net Worth</div>
                        </div>
                        ''' if summary.retirement_401k_value > 0 else ''}
                    </div>
                    
                    <div style="text-align: center; margin: 20px 0;">
                        <span class="status-badge" style="background-color: {status_color}; color: white;">
                            {status_text}
                        </span>
                        <p style="margin: 10px 0 0 0; color: #6c757d;">
                            Max Drift: {format_percent(summary.max_drift)} ({summary.max_drift_symbol}) | Threshold: ±{format_percent(summary.breach_threshold)}
                        </p>
                    </div>
                    
                    <h3 style="border-bottom: 2px solid #667eea; padding-bottom: 10px;">Recommended Actions</h3>
                    <p style="font-weight: bold; color: #495057;">{recommendations.summary_message}</p>
                    {recs_html if recs_html else '<p style="color: #6c757d;">No actions required at this time.</p>'}
                    
                    {notes_html}
                </div>
                
                <div class="footer">
                    <p style="margin: 0;">This is an automated report. Not financial advice.</p>
                    <p style="margin: 5px 0 0 0;">Portfolio Automation System</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        return html


def create_email_reporter(config: dict) -> EmailReporter:
    """Factory function to create email reporter from config."""
    return EmailReporter(
        smtp_server=config.get('smtp_server', 'smtp.gmail.com'),
        smtp_port=config.get('smtp_port', 587),
        use_tls=config.get('use_tls', True),
        sender_email=config.get('sender_email'),
        recipient_email=config.get('recipient_email')
    )


def should_send_report(
    schedule_config: dict,
    summary: PortfolioSummary
) -> tuple[bool, bool, bool]:
    """
    Determine if report should be sent based on schedule.
    Returns (should_send, is_weekly, is_annual).
    """
    is_weekly = False
    is_annual = False
    should_send = False
    
    # Check weekly summary
    if schedule_config.get('weekly_summary_enabled', False):
        weekly_day = schedule_config.get('weekly_summary_day', 'sunday')
        if is_weekly_summary_day(weekly_day):
            is_weekly = True
            should_send = True
    
    # Check annual review
    if schedule_config.get('annual_review_enabled', False):
        annual_date = schedule_config.get('annual_review_date', '01-03')
        if is_annual_review_date(annual_date):
            is_annual = True
            should_send = True
    
    # Always send if rebalance needed
    if summary.has_breach:
        should_send = True
    
    return should_send, is_weekly, is_annual
