"""
Factor attribution — regress a tactic's monthly excess returns on Fama-French
factors (Mkt-RF, SMB, HML, RMW, CMA, + MOM) to explain WHY it beat or lagged SPY:
true tactic value (alpha) vs factor exposure (e.g. just overweight tech/growth).

numpy least-squares (no new dependency). Degrades to `factor_data_unavailable`
when the factor cache is absent.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from portfolio_automation.portfolio_sim.factor_data import available_factors


def attribute(
    tactic_monthly: dict[str, float],
    factors: dict[str, dict[str, float]],
) -> dict[str, Any]:
    """
    `tactic_monthly` = {month 'YYYY-MM': tactic_return_decimal}. Regress excess
    (tactic − RF) on the available factors. Returns alpha (annualized), betas, R².
    """
    if not factors:
        return {"status": "factor_data_unavailable"}
    facs = available_factors(factors)
    if not facs:
        return {"status": "factor_data_unavailable"}

    months = sorted(set(tactic_monthly) & set(factors))
    if len(months) < len(facs) + 3:
        return {"status": "insufficient_data", "n_months": len(months)}

    y = np.array([tactic_monthly[m] - factors[m].get("RF", 0.0) for m in months])
    X = np.array([[factors[m].get(f, 0.0) for f in facs] for m in months])
    X1 = np.column_stack([np.ones(len(months)), X])   # intercept = alpha
    coef, _res, _rank, _sv = np.linalg.lstsq(X1, y, rcond=None)
    alpha_m = float(coef[0])
    betas = {f: round(float(b), 4) for f, b in zip(facs, coef[1:])}
    resid = y - X1 @ coef
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((resid ** 2).sum()) / ss_tot if ss_tot > 0 else 0.0
    return {
        "status": "ok",
        "n_months": len(months),
        "alpha_monthly": round(alpha_m, 6),
        "alpha_annualized": round((1 + alpha_m) ** 12 - 1, 6),
        "betas": betas,
        "r_squared": round(r2, 4),
        "interpretation": ("excess looks factor-driven (low alpha, high beta/R²)"
                           if abs(alpha_m) < 0.002 and r2 > 0.7
                           else "some idiosyncratic alpha beyond factor exposure"),
    }


def build_factor_report(per_tactic: dict[str, dict[str, Any]], *, run_id: str, run_mode: str,
                        factors_available: bool) -> dict[str, Any]:
    from portfolio_automation.portfolio_sim.sim_base import sim_envelope, SimStatus
    status = SimStatus.OK.value if factors_available else SimStatus.DEGRADED.value
    env = sim_envelope(run_id=run_id, run_mode=run_mode, status=status,
                       warnings=[] if factors_available else ["factor_data_unavailable"])
    return {**env, "factor_data_available": factors_available, "records": per_tactic}


def render_factor_md(report: dict[str, Any]) -> str:
    lines = ["# Factor Attribution — Sandbox", "",
             "_Did a tactic beat SPY from true value (alpha) or just factor exposure?_", ""]
    if not report.get("factor_data_available"):
        lines.append("_Factor data unavailable — run scripts/fetch_factor_data.sh._")
        return "\n".join(lines)
    for tid, a in (report.get("records") or {}).items():
        if a.get("status") != "ok":
            continue
        lines.append(f"- **{tid}**: alpha {a['alpha_annualized']:+.2%}/yr · "
                     f"R² {a['r_squared']:.2f} · betas {a['betas']}")
    return "\n".join(lines)
