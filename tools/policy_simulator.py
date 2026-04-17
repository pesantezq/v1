from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.policy_framework import (
    POLICY_REGISTRY,
    STRATEGY_PROFILES,
    PolicyDefinition,
    StrategyProfile,
    apply_policy_definition,
    get_policy,
    get_profile,
    list_policy_names,
    list_profile_names,
    policy_degraded_compatibility,
    policy_filters_summary,
    policy_regime_preference_summary,
    resolve_requested_policies,
)

DEFAULT_INPUT_CSV = Path("outputs/performance/signal_outcomes.csv")
DEFAULT_OUTPUT_DIR = Path("outputs/simulations")
DEFAULT_DB_PATH = Path("data/portfolio.db")

# Backward-compatible exports for existing tests/imports.
POLICIES = POLICY_REGISTRY
PROFILES = STRATEGY_PROFILES


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


def _read_rows_from_csv(path: Path) -> list[dict[str, Any]]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _read_rows_from_db(path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM watchlist_signal_feedback ORDER BY signal_time DESC, id DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def load_historical_dataset(
    *,
    input_csv: Path = DEFAULT_INPUT_CSV,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    if input_csv.exists():
        return _read_rows_from_csv(input_csv)
    if db_path.exists():
        return _read_rows_from_db(db_path)
    raise FileNotFoundError(
        f"No historical dataset found at {input_csv} or {db_path}"
    )


def _normalize_row(row: dict[str, Any], primary_window_days: int) -> dict[str, Any]:
    normalized = dict(row)
    return_col = f"outcome_return_{primary_window_days}d"
    success_col = f"outcome_success_{primary_window_days}d"
    normalized["return_pct"] = _safe_float(row.get(return_col), 0.0)
    normalized["outcome_success"] = int(_safe_float(row.get(success_col), 0.0) or 0)
    normalized["conviction_score"] = _safe_float(row.get("conviction_score"), 0.0)
    normalized["normalized_allocation"] = _safe_float(row.get("normalized_allocation"), 0.0)
    normalized["signal_score"] = _safe_float(row.get("signal_score"), 0.0)
    normalized["confidence_score"] = _safe_float(row.get("confidence_score"), 0.0)
    normalized["regime_confidence"] = _safe_float(row.get("regime_confidence"), 0.0)
    normalized["degraded_mode"] = _parse_bool(row.get("degraded_mode"))
    normalized["signal_time"] = str(row.get("signal_time") or "")
    normalized["regime_label"] = str(row.get("regime_label") or "neutral")
    normalized["conviction_band"] = str(row.get("conviction_band") or "observe")
    normalized["signal_reliability"] = str(row.get("signal_reliability") or "unproven")
    normalized["regime_data_quality"] = str(row.get("regime_data_quality") or "limited")
    normalized["sector"] = str(row.get("sector") or "")
    return normalized


def _apply_global_filters(
    rows: list[dict[str, Any]],
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    regime: str | None = None,
    sector: str | None = None,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for row in rows:
        signal_time = str(row.get("signal_time") or "")
        signal_date = signal_time[:10] if len(signal_time) >= 10 else ""
        if date_from and signal_date and signal_date < date_from:
            continue
        if date_to and signal_date and signal_date > date_to:
            continue
        if regime and str(row.get("regime_label") or "").lower() != regime.lower():
            continue
        if sector and str(row.get("sector") or "").lower() != sector.lower():
            continue
        filtered.append(dict(row))
    return filtered


def apply_policy(rows: list[dict[str, Any]], policy: PolicyDefinition) -> list[dict[str, Any]]:
    return apply_policy_definition(rows, policy)


def _equity_curve(returns: list[float]) -> tuple[float, float]:
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for return_pct in returns:
        equity *= 1.0 + (return_pct / 100.0)
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = min(max_drawdown, (equity - peak) / peak)
    return round((equity - 1.0) * 100.0, 3), round(abs(max_drawdown) * 100.0, 3)


def _performance_by_regime(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_regime: dict[str, Any] = {}
    regime_groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        regime_groups.setdefault(str(row.get("regime_label") or "neutral"), []).append(row)
    for regime_label, regime_rows in sorted(regime_groups.items()):
        regime_returns = [float(row.get("return_pct") or 0.0) for row in regime_rows]
        by_regime[regime_label] = {
            "total_trades": len(regime_rows),
            "win_rate": round(
                sum(1 for row in regime_rows if int(row.get("outcome_success") or 0) == 1) / len(regime_rows),
                3,
            ) if regime_rows else 0.0,
            "avg_return_pct": round(sum(regime_returns) / len(regime_rows), 3) if regime_rows else 0.0,
        }
    return by_regime


def summarize_policy(
    rows: list[dict[str, Any]],
    *,
    policy: PolicyDefinition,
    requested_profiles: list[StrategyProfile] | None = None,
) -> dict[str, Any]:
    total_trades = len(rows)
    returns = [float(row.get("return_pct") or 0.0) for row in rows]
    weighted_returns = [
        float(row.get("return_pct") or 0.0) * float(row.get("simulated_allocation") or 0.0)
        for row in rows
    ]
    win_rate = round(
        sum(1 for row in rows if int(row.get("outcome_success") or 0) == 1) / total_trades,
        3,
    ) if total_trades else 0.0
    avg_return = round(sum(returns) / total_trades, 3) if total_trades else 0.0
    cumulative_return, max_drawdown = _equity_curve(returns)
    weighted_cumulative_return, weighted_max_drawdown = _equity_curve(weighted_returns)
    degraded_rows = [row for row in rows if bool(row.get("degraded_mode"))]
    degraded_win_rate = round(
        sum(1 for row in degraded_rows if int(row.get("outcome_success") or 0) == 1) / len(degraded_rows),
        3,
    ) if degraded_rows else None
    degraded_avg_return = round(
        sum(float(row.get("return_pct") or 0.0) for row in degraded_rows) / len(degraded_rows),
        3,
    ) if degraded_rows else None
    associated_profiles = [
        profile.name
        for profile in (requested_profiles or [])
        if policy.name in profile.policy_bundle
    ]

    return {
        "policy": policy.name,
        "description": policy.description,
        "category": policy.category,
        "rationale": policy.rationale,
        "intended_use_case": policy.intended_use_case,
        "strategy_profiles": associated_profiles,
        "filters_applied": policy_filters_summary(policy),
        "sizing_modifier_logic": (
            f"multiplier={policy.allocation_multiplier:.2f}, "
            f"max_allocation={policy.max_allocation if policy.max_allocation is not None else 'none'}, "
            f"degraded_multiplier={policy.degraded_allocation_multiplier if policy.degraded_allocation_multiplier is not None else 'none'}"
        ),
        "regime_preference_summary": policy_regime_preference_summary(policy),
        "degraded_mode_compatibility": policy_degraded_compatibility(policy),
        "minimum_data_quality_expectations": list(policy.minimum_data_quality),
        "total_trades": total_trades,
        "win_rate": win_rate,
        "avg_return_pct": avg_return,
        "cumulative_return_pct": cumulative_return,
        "max_drawdown_pct": max_drawdown,
        "weighted_cumulative_return_pct": weighted_cumulative_return,
        "weighted_max_drawdown_pct": weighted_max_drawdown,
        "performance_by_regime": _performance_by_regime(rows),
        "degraded_mode_stats": {
            "total_trades": len(degraded_rows),
            "win_rate": degraded_win_rate,
            "avg_return_pct": degraded_avg_return,
        },
    }


def _comparison_summary(
    policy_results: list[dict[str, Any]],
    selected_profiles: list[StrategyProfile],
) -> dict[str, Any]:
    if not policy_results:
        return {
            "best_by_win_rate": None,
            "best_by_drawdown": None,
            "best_degraded_mode_policy": None,
            "best_policy_by_regime": {},
            "best_policy_by_profile": {},
        }

    def _max_by(key: str) -> str | None:
        sortable = [item for item in policy_results if item.get(key) is not None]
        if not sortable:
            return None
        return max(sortable, key=lambda item: float(item.get(key) or 0.0)).get("policy")

    def _min_by(key: str) -> str | None:
        sortable = [item for item in policy_results if item.get(key) is not None]
        if not sortable:
            return None
        return min(sortable, key=lambda item: float(item.get(key) or 0.0)).get("policy")

    best_by_regime: dict[str, str] = {}
    regimes = {
        regime
        for item in policy_results
        for regime in (item.get("performance_by_regime") or {}).keys()
    }
    for regime in sorted(regimes):
        candidates = [
            item for item in policy_results
            if regime in (item.get("performance_by_regime") or {})
        ]
        if not candidates:
            continue
        best = max(
            candidates,
            key=lambda item: float((item.get("performance_by_regime") or {}).get(regime, {}).get("win_rate") or 0.0),
        )
        best_by_regime[regime] = str(best.get("policy") or "")

    best_by_profile: dict[str, str] = {}
    for profile in selected_profiles:
        candidates = [
            item for item in policy_results
            if profile.name in (item.get("strategy_profiles") or [])
        ]
        if not candidates:
            continue
        best = max(candidates, key=lambda item: float(item.get("win_rate") or 0.0))
        best_by_profile[profile.name] = str(best.get("policy") or "")

    degraded_candidates = [
        item for item in policy_results
        if (item.get("degraded_mode_stats") or {}).get("win_rate") is not None
    ]
    best_degraded = None
    if degraded_candidates:
        best_degraded = max(
            degraded_candidates,
            key=lambda item: float((item.get("degraded_mode_stats") or {}).get("win_rate") or 0.0),
        ).get("policy")

    return {
        "best_by_win_rate": _max_by("win_rate"),
        "best_by_drawdown": _min_by("max_drawdown_pct"),
        "best_degraded_mode_policy": best_degraded,
        "best_policy_by_regime": best_by_regime,
        "best_policy_by_profile": best_by_profile,
    }


def run_policy_simulation(
    *,
    policies: list[str] | None = None,
    profiles: list[str] | None = None,
    input_csv: Path = DEFAULT_INPUT_CSV,
    db_path: Path = DEFAULT_DB_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    primary_window_days: int = 3,
    date_from: str | None = None,
    date_to: str | None = None,
    regime: str | None = None,
    sector: str | None = None,
) -> dict[str, Any]:
    raw_rows = load_historical_dataset(input_csv=input_csv, db_path=db_path)
    dataset = [_normalize_row(row, primary_window_days) for row in raw_rows]
    dataset = [row for row in dataset if row.get("return_pct") is not None]
    filtered_dataset = _apply_global_filters(
        dataset,
        date_from=date_from,
        date_to=date_to,
        regime=regime,
        sector=sector,
    )

    resolved_policies, resolved_profiles = resolve_requested_policies(
        policy_names=list(policies or []),
        profile_names=list(profiles or []),
    )
    if not resolved_policies:
        raise RuntimeError("At least one --policy or --profile is required.")

    results: list[dict[str, Any]] = []
    for policy in resolved_policies:
        simulated_rows = apply_policy(filtered_dataset, policy)
        results.append(
            summarize_policy(
                simulated_rows,
                policy=policy,
                requested_profiles=resolved_profiles,
            )
        )

    summary = {
        "generated_at": datetime.now().isoformat(),
        "primary_window_days": primary_window_days,
        "dataset_size": len(dataset),
        "filtered_dataset_size": len(filtered_dataset),
        "filters": {
            "date_from": date_from,
            "date_to": date_to,
            "regime": regime,
            "sector": sector,
        },
        "requested_policies": [policy.name for policy in resolved_policies],
        "requested_profiles": [profile.name for profile in resolved_profiles],
        "profiles": [
            {
                "name": profile.name,
                "description": profile.description,
                "policy_bundle": list(profile.policy_bundle),
                "preferred_conviction_bands": list(profile.preferred_conviction_bands),
                "regime_preferences": list(profile.regime_preferences),
                "degraded_mode_tolerance": profile.degraded_mode_tolerance,
                "max_suggested_size_style": profile.max_suggested_size_style,
                "avoid_risk_off": profile.avoid_risk_off,
                "allow_starter_ideas": profile.allow_starter_ideas,
            }
            for profile in resolved_profiles
        ],
        "policies": results,
    }
    summary["comparison"] = _comparison_summary(results, resolved_profiles)

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "policy_simulation.json"
    md_path = output_dir / "policy_simulation.md"
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    md_path.write_text(render_policy_simulation_markdown(summary), encoding="utf-8")
    summary["paths"] = {
        "json_path": str(json_path),
        "markdown_path": str(md_path),
    }
    return summary


def render_policy_simulation_markdown(summary: dict[str, Any]) -> str:
    comparison = dict(summary.get("comparison") or {})
    lines = [
        "# Policy Simulation",
        "",
        f"Generated: {summary.get('generated_at', '')}  ",
        f"Dataset size: **{int(summary.get('dataset_size') or 0)}**  ",
        f"Filtered dataset size: **{int(summary.get('filtered_dataset_size') or 0)}**  ",
        f"Profiles requested: **{', '.join(summary.get('requested_profiles') or ['none'])}**  ",
        "",
        "## Strategy View",
        "",
        f"- Best recent policy by win rate: `{comparison.get('best_by_win_rate') or 'n/a'}`",
        f"- Best recent policy by drawdown: `{comparison.get('best_by_drawdown') or 'n/a'}`",
        f"- Best policy under degraded mode: `{comparison.get('best_degraded_mode_policy') or 'n/a'}`",
    ]
    best_by_regime = dict(comparison.get("best_policy_by_regime") or {})
    if best_by_regime:
        for regime_name, policy_name in best_by_regime.items():
            lines.append(f"- Best policy for {regime_name}: `{policy_name}`")

    lines += [
        "",
        "## Comparison",
        "",
        "| Policy | Category | Profiles | Trades | Win Rate | Avg Return | Cumulative | Max Drawdown |",
        "|--------|----------|----------|--------|----------|------------|------------|--------------|",
    ]
    for policy in summary.get("policies", []) or []:
        lines.append(
            f"| {policy.get('policy', '')} | {policy.get('category', '')} | "
            f"{', '.join(policy.get('strategy_profiles') or []) or '-'} | "
            f"{int(policy.get('total_trades') or 0)} | "
            f"{float(policy.get('win_rate') or 0.0):.1%} | "
            f"{float(policy.get('avg_return_pct') or 0.0):+.2f}% | "
            f"{float(policy.get('cumulative_return_pct') or 0.0):+.2f}% | "
            f"{float(policy.get('max_drawdown_pct') or 0.0):.2f}% |"
        )
    lines.append("")

    selected_profiles = {
        profile.get("name", ""): profile
        for profile in summary.get("profiles", []) or []
    }
    if selected_profiles:
        lines += ["## Profiles", ""]
        for name, profile in selected_profiles.items():
            lines.append(
                f"- `{name}`: {profile.get('description', '')} "
                f"(policies: {', '.join(profile.get('policy_bundle') or [])})"
            )
        lines.append("")

    for policy in summary.get("policies", []) or []:
        lines.append(f"## {policy.get('policy', '')}")
        lines.append("")
        lines.append(policy.get("description", ""))
        lines.append("")
        lines.append(f"- Category: `{policy.get('category', '')}`")
        lines.append(f"- Intended use case: {policy.get('intended_use_case', '')}")
        lines.append(f"- Regime preference: {policy.get('regime_preference_summary', '')}")
        lines.append(f"- Degraded-mode compatibility: {policy.get('degraded_mode_compatibility', '')}")
        lines.append(f"- Minimum data quality: {', '.join(policy.get('minimum_data_quality_expectations') or [])}")
        lines.append(f"- Filters applied: {', '.join(policy.get('filters_applied') or [])}")
        lines.append(f"- Sizing logic: {policy.get('sizing_modifier_logic', '')}")
        by_regime = dict(policy.get("performance_by_regime") or {})
        if by_regime:
            lines.append("")
            lines.append("| Regime | Trades | Win Rate | Avg Return |")
            lines.append("|--------|--------|----------|------------|")
            for regime_label, metrics in by_regime.items():
                lines.append(
                    f"| {regime_label} | {int(metrics.get('total_trades') or 0)} | "
                    f"{float(metrics.get('win_rate') or 0.0):.1%} | "
                    f"{float(metrics.get('avg_return_pct') or 0.0):+.2f}% |"
                )
        lines.append("")
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline policy simulator for historical signal outcomes.")
    parser.add_argument("--policy", action="append", dest="policies", help="Policy name to simulate. Can be repeated.")
    parser.add_argument("--profile", action="append", dest="profiles", help="Strategy profile to simulate. Can be repeated.")
    parser.add_argument("--list-policies", action="store_true", help="Print available policy names and exit.")
    parser.add_argument("--list-profiles", action="store_true", help="Print available profile names and exit.")
    parser.add_argument("--input-csv", default=str(DEFAULT_INPUT_CSV), help="Historical dataset CSV path.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Fallback SQLite path if CSV is unavailable.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for simulation outputs.")
    parser.add_argument("--primary-window-days", type=int, default=3, help="Outcome window to evaluate.")
    parser.add_argument("--date-from", default=None, help="Optional start date filter (YYYY-MM-DD).")
    parser.add_argument("--date-to", default=None, help="Optional end date filter (YYYY-MM-DD).")
    parser.add_argument("--regime", default=None, help="Optional regime filter.")
    parser.add_argument("--sector", default=None, help="Optional sector filter when available in the dataset.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.list_policies:
        for name in list_policy_names():
            print(name)
        return
    if args.list_profiles:
        for name in list_profile_names():
            print(name)
        return
    summary = run_policy_simulation(
        policies=list(args.policies or []),
        profiles=list(args.profiles or []),
        input_csv=Path(args.input_csv),
        db_path=Path(args.db_path),
        output_dir=Path(args.output_dir),
        primary_window_days=int(args.primary_window_days),
        date_from=args.date_from,
        date_to=args.date_to,
        regime=args.regime,
        sector=args.sector,
    )
    print(f"Policy simulation written: {summary['paths']['json_path']}")
    print(f"                         {summary['paths']['markdown_path']}")


if __name__ == "__main__":
    main()
