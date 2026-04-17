from __future__ import annotations

import argparse
import json
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from gui_operator_data import load_operator_dashboard_data
from utils import get_env


DEFAULT_OUTPUT_PATH = Path("outputs/reports/weekly_summary.md")


@dataclass
class WeeklyReportResult:
    markdown: str
    plain_text: str
    html: str
    output_path: Path
    sections: list[str]
    sent_email: bool = False


def _safe_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _fmt_pct(value: Any) -> str:
    try:
        if value is None:
            return "Unknown"
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "Unknown"


def _fmt_ret(value: Any) -> str:
    try:
        if value is None:
            return "Unknown"
        number = float(value)
        sign = "+" if number >= 0 else ""
        return f"{sign}{number * 100:.1f}%"
    except (TypeError, ValueError):
        return "Unknown"


def _first_or_default(items: list[str], default: str) -> str:
    return items[0] if items else default


def _build_signal_quality_section(bundle: dict[str, Any]) -> tuple[list[str], list[str]]:
    triage = bundle.get("signal_triage", {})
    regime = bundle.get("regime_analytics_view", {})
    counts = triage.get("counts_by_band", {}) or {}
    rows = regime.get("rows", []) or []

    lines = ["## Signal Quality", ""]
    takeaways: list[str] = []

    if not counts and not rows:
        lines.append("- No signal quality data available yet.")
        lines.append("")
        takeaways.append("Signal quality history is still sparse; monitor for more resolved outcomes.")
        return lines, takeaways

    high_count = counts.get("high_conviction", 0)
    low_count = counts.get("observe", 0) + counts.get("defer", 0) + counts.get("suppressed", 0)
    lines.append(f"- Current conviction mix: high conviction `{high_count}`, lower-priority `{low_count}`.")

    if rows:
        bests = ", ".join(f"{row['regime']}: {row['best_conviction_band']}" for row in rows[:3])
        worsts = ", ".join(f"{row['regime']}: {row['worst_conviction_band']}" for row in rows[:3])
        lines.append(f"- Historical best conviction bands by regime: {bests}.")
        lines.append(f"- Historical weakest conviction bands by regime: {worsts}.")
        if any(row.get("best_conviction_band") == "high_conviction" for row in rows):
            takeaways.append("Higher-conviction signals are showing up as the best historical band in at least one regime.")
        if any(row.get("worst_conviction_band") in {"observe", "defer", "starter"} for row in rows):
            takeaways.append("Lower-conviction bands remain the weakest historical area and deserve tighter monitoring.")
    else:
        lines.append("- No resolved regime history is available yet for conviction-band comparisons.")
        takeaways.append("Conviction-band performance still lacks enough resolved history for a strong weekly read.")

    lines.append("")
    return lines, takeaways


def _build_performance_section(bundle: dict[str, Any]) -> tuple[list[str], list[str]]:
    performance = bundle.get("performance_view", {})
    distribution = performance.get("return_distribution", {}) or {}
    lines = ["## Performance Highlights", ""]
    takeaways: list[str] = []

    if not performance.get("available"):
        lines.append("- No performance analytics available.")
        lines.append("")
        takeaways.append("Performance analytics remain unavailable; coverage is still building.")
        return lines, takeaways

    lines.extend(
        [
            f"- Overall hit rate: `{_fmt_pct(distribution.get('hit_rate'))}`.",
            f"- Average return (5d): `{_fmt_ret(distribution.get('avg_return_5d'))}`.",
            f"- Strong win rate: `{_fmt_pct(distribution.get('strong_win_rate'))}`.",
            f"- Adverse rate: `{_fmt_pct(distribution.get('adverse_rate'))}`.",
            f"- Sample quality: `{performance.get('sample_quality', 'unknown')}` with `{performance.get('sample_size', 0)}` attributable records.",
        ]
    )

    if distribution.get("adverse_rate") is not None and float(distribution["adverse_rate"]) > 0.2:
        takeaways.append("Adverse outcomes are elevated enough to deserve extra operator attention.")
    else:
        takeaways.append("Headline performance remains constructive if the current hit/adverse mix holds.")
    lines.append("")
    return lines, takeaways


def _build_calibration_section(bundle: dict[str, Any]) -> tuple[list[str], list[str]]:
    quality = bundle.get("recommendation_quality_view", {})
    monotonicity = quality.get("monotonicity_label", "unavailable")
    notes = quality.get("notes", []) or []
    lines = ["## Calibration Insights", ""]
    takeaways: list[str] = []

    lines.append(f"- Confidence monotonicity status: `{monotonicity}`.")
    if notes:
        for note in notes[:3]:
            lines.append(f"- {note}")

    if monotonicity == "monotonic":
        takeaways.append("Confidence calibration is behaving as expected: higher-confidence buckets are outperforming lower ones.")
    elif monotonicity == "mixed":
        takeaways.append("Confidence calibration is mixed, so operators should rely more heavily on sample size and context.")
    elif monotonicity == "inverted":
        takeaways.append("Confidence calibration looks inverted and should be treated as a review priority.")
    else:
        takeaways.append("Calibration is not yet evaluable because the analytics sample is still thin.")

    lines.append("")
    return lines, takeaways


def _build_regime_section(bundle: dict[str, Any]) -> tuple[list[str], list[str]]:
    regime = bundle.get("regime_analytics_view", {})
    lines = ["## Regime Insights", ""]
    takeaways: list[str] = []

    if not regime.get("available"):
        lines.append("- No regime analytics available.")
        lines.append("")
        takeaways.append("Regime analytics are still sparse, so keep interpreting regime conclusions cautiously.")
        return lines, takeaways

    for row in regime.get("rows", [])[:5]:
        lines.append(
            f"- `{row['regime']}`: win rate `{_fmt_pct(row.get('win_rate'))}`, "
            f"avg return `{row.get('avg_return_pct', 'Unknown')}%` if available, "
            f"best band `{row.get('best_conviction_band', 'n/a')}`, worst band `{row.get('worst_conviction_band', 'n/a')}`."
        )
        if row.get("degraded_note"):
            lines.append(f"  Degraded-data note: {row['degraded_note']}")

    negative_rows = [row for row in regime.get("rows", []) if isinstance(row.get("avg_return_pct"), (int, float)) and row["avg_return_pct"] < 0]
    if negative_rows:
        takeaways.append(
            "At least one regime is underperforming on realized returns, so regime context should stay in the weekly operator review."
        )
    else:
        takeaways.append("Current regime history does not show a broad breakdown across the tracked buckets.")
    lines.append("")
    return lines, takeaways


def _build_recommendation_quality_section(bundle: dict[str, Any]) -> tuple[list[str], list[str]]:
    quality = bundle.get("recommendation_quality_view", {})
    lines = ["## Recommendation Quality", ""]
    takeaways: list[str] = []
    degraded_rows = quality.get("by_degraded_mode", []) or []
    action_rows = quality.get("by_action_level", []) or []
    impact_rows = quality.get("by_impact_area", []) or []
    decile_rows = quality.get("by_score_decile", []) or []

    if not quality.get("available") or not any([degraded_rows, action_rows, impact_rows, decile_rows]):
        lines.append("- No recommendation quality analytics available.")
        lines.append("")
        takeaways.append("Recommendation quality analytics are still missing or too sparse to summarize.")
        return lines, takeaways

    if degraded_rows:
        lines.append("- Hit rate by degraded vs normal:")
        for row in degraded_rows:
            lines.append(
                f"  - `{row['bucket']}`: hit rate `{_fmt_pct(row.get('hit_rate'))}` on `{row.get('attributable_count', 0)}` attributable records."
            )

    if action_rows:
        lines.append("- Action-level performance:")
        for row in action_rows[:4]:
            lines.append(
                f"  - `{row['bucket']}`: hit rate `{_fmt_pct(row.get('hit_rate'))}`, avg 5d `{_fmt_ret(row.get('avg_return_5d'))}`."
            )

    if impact_rows:
        lines.append("- Impact-area performance:")
        for row in impact_rows[:4]:
            lines.append(
                f"  - `{row['bucket']}`: hit rate `{_fmt_pct(row.get('hit_rate'))}`, avg 5d `{_fmt_ret(row.get('avg_return_5d'))}`."
            )

    if degraded_rows and len(degraded_rows) >= 2:
        normal = next((row for row in degraded_rows if row["bucket"] == "normal"), None)
        degraded = next((row for row in degraded_rows if row["bucket"] == "degraded"), None)
        if normal and degraded and normal.get("hit_rate") is not None and degraded.get("hit_rate") is not None:
            if float(normal["hit_rate"]) > float(degraded["hit_rate"]):
                takeaways.append("Normal-mode recommendations are outperforming degraded-mode recommendations in current tracked outcomes.")
            else:
                takeaways.append("Degraded-mode recommendations are not lagging normal-mode outcomes right now, but sample size still matters.")

    lines.append("")
    return lines, takeaways


def _build_executive_section(bundle: dict[str, Any]) -> tuple[list[str], list[str]]:
    overview = bundle.get("overview", {})
    warnings = overview.get("top_warnings", []) or []
    lines = [
        "# Weekly Operator Review",
        "",
        f"_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
        "## Executive Summary",
        "",
        f"- Current regime: `{overview.get('market_regime', 'Unknown')}` at `{_fmt_pct(overview.get('market_regime_confidence'))}` confidence.",
        f"- Degraded mode: `{'on' if overview.get('degraded_mode') else 'off'}`"
        + (f" (`{overview.get('degraded_reason')}`)." if overview.get("degraded_reason") else "."),
        f"- Current recommendation: policy `{overview.get('policy', 'Unavailable')}`, profile `{overview.get('profile', 'Unavailable')}`.",
        f"- Key warning: {_first_or_default(warnings, 'No major warning surfaced this week.')}",
        "",
    ]
    takeaways = [
        f"Current system posture is `{overview.get('market_regime', 'unknown')}` with recommendation `{overview.get('profile', 'Unavailable')}` / `{overview.get('policy', 'Unavailable')}`."
    ]
    return lines, takeaways


def _dedupe_takeaways(items: list[str], limit: int = 5) -> list[str]:
    output: list[str] = []
    seen = set()
    for item in items:
        normalized = item.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            output.append(normalized)
        if len(output) >= limit:
            break
    return output


def build_weekly_summary_markdown(bundle: dict[str, Any]) -> str:
    sections: list[str] = []
    takeaways: list[str] = []

    for builder in (
        _build_executive_section,
        _build_signal_quality_section,
        _build_performance_section,
        _build_calibration_section,
        _build_regime_section,
        _build_recommendation_quality_section,
    ):
        lines, notes = builder(bundle)
        sections.extend(lines)
        takeaways.extend(notes)

    sections.append("## Key Takeaways")
    sections.append("")
    for item in _dedupe_takeaways(takeaways, limit=5):
        sections.append(f"- {item}")
    sections.append("")
    sections.append("_Advisory only - this report does not change live investing behavior._")
    sections.append("")
    return "\n".join(sections)


def markdown_to_plain_text(markdown: str) -> str:
    text = markdown.replace("`", "")
    text = text.replace("# ", "").replace("## ", "")
    text = text.replace("_", "")
    return text.strip() + "\n"


def build_weekly_summary_html(markdown: str) -> str:
    lines = markdown.splitlines()
    html_parts = [
        "<html><body style='font-family:Segoe UI,Arial,sans-serif;line-height:1.5;color:#16202b;'>"
    ]
    in_list = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            continue
        if stripped.startswith("# "):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append(f"<h1>{stripped[2:]}</h1>")
        elif stripped.startswith("## "):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("- "):
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            html_parts.append(f"<li>{stripped[2:].replace('`', '')}</li>")
        else:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append(f"<p>{stripped.replace('`', '')}</p>")
    if in_list:
        html_parts.append("</ul>")
    html_parts.append("</body></html>")
    return "\n".join(html_parts)


def load_weekly_review(root: Path | str) -> dict[str, Any]:
    root_path = Path(root)
    report_path = root_path / DEFAULT_OUTPUT_PATH
    exists = report_path.exists()
    markdown = report_path.read_text(encoding="utf-8") if exists else ""
    return {
        "available": exists,
        "path": report_path,
        "relative_path": str(report_path.relative_to(root_path)),
        "markdown": markdown,
        "updated_at": datetime.fromtimestamp(report_path.stat().st_mtime).isoformat() if exists else None,
    }


def send_weekly_summary_email(
    *,
    config: dict[str, Any],
    plain_text: str,
    html: str,
    subject: str | None = None,
) -> bool:
    email_cfg = config.get("email", {}) if isinstance(config, dict) else {}
    sender = email_cfg.get("sender_email") or get_env("EMAIL_SENDER")
    recipient = email_cfg.get("recipient_email") or get_env("EMAIL_RECIPIENT")
    password = get_env("EMAIL_PASSWORD")
    smtp_server = email_cfg.get("smtp_server", "smtp.gmail.com")
    smtp_port = int(email_cfg.get("smtp_port", 587))
    use_tls = bool(email_cfg.get("use_tls", True))

    if not all([sender, recipient, password]):
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject or f"Weekly Operator Review - {datetime.now():%Y-%m-%d}"
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_server, smtp_port) as server:
        if use_tls:
            server.starttls(context=context)
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())
    return True


def generate_weekly_summary(
    *,
    root: Path | str = ".",
    output_path: Path | str | None = None,
    write_output: bool = True,
) -> WeeklyReportResult:
    root_path = Path(root)
    bundle = load_operator_dashboard_data(root_path)
    markdown = build_weekly_summary_markdown(bundle)
    plain_text = markdown_to_plain_text(markdown)
    html = build_weekly_summary_html(markdown)
    target = root_path / (Path(output_path) if output_path else DEFAULT_OUTPUT_PATH)
    if write_output:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
    sections = [
        "Executive Summary",
        "Signal Quality",
        "Performance Highlights",
        "Calibration Insights",
        "Regime Insights",
        "Recommendation Quality",
        "Key Takeaways",
    ]
    return WeeklyReportResult(
        markdown=markdown,
        plain_text=plain_text,
        html=html,
        output_path=target,
        sections=sections,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a read-only weekly operator review report.")
    parser.add_argument("--root", default=".", help="Repo root to read artifacts from.")
    parser.add_argument("--output-path", default=str(DEFAULT_OUTPUT_PATH), help="Markdown output path relative to root.")
    parser.add_argument("--output-only", action="store_true", help="Generate the report without attempting email send.")
    parser.add_argument("--send-email", action="store_true", help="Send the generated report using config/email settings.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    root = Path(args.root).resolve()
    result = generate_weekly_summary(root=root, output_path=args.output_path, write_output=True)

    print(f"Weekly summary written: {result.output_path}")
    if args.send_email and not args.output_only:
        config = _safe_json(root / "config.json")
        sent = send_weekly_summary_email(
            config=config,
            plain_text=result.plain_text,
            html=result.html,
        )
        print("Email sent." if sent else "Email not sent; configuration incomplete.")
        return 0 if sent else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
