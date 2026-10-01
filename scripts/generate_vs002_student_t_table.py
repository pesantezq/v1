#!/usr/bin/env python3
"""Deterministic generator + provenance tool for the VS-002 Student-t table.

This is the REVIEWABLE GENERATION / VERIFICATION mechanism for the committed
static table in ``portfolio_automation/vs002_evidence/result_contract.py``. It is
NOT imported by the result runner at runtime: normal result-runner execution
reads the committed static table only (``student_t_critical`` is a pure lookup).

It computes the two-sided 95% (upper 0.975) Student-t critical values by pure-
python regularized-incomplete-beta inversion — no scipy/statsmodels/numpy, no
network, no runtime dependency. Run ``python scripts/generate_vs002_student_t_table.py``
to print the literal for the committed table, or ``... verify`` to check the
committed table reproduces from this method.

Table domain: df 1..CERTIFIED_MAX_DF. This is CERTIFIED STATIC TABLE COVERAGE
(implementation coverage), NOT a scientific maximum cohort count, and nothing
about the real evidence package informs it.
"""
from __future__ import annotations

import math
import sys

CERTIFIED_MAX_DF = 1000
STORED_DECIMALS = 8


def _regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b) via the Lentz continued fraction
    (Numerical Recipes ``betai``). Pure-python, deterministic."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(ln_beta + a * math.log(x) + b * math.log(1.0 - x))

    def _betacf(a: float, b: float, x: float) -> float:
        tiny = 1e-300
        qab = a + b
        qap = a + 1.0
        qam = a - 1.0
        c = 1.0
        d = 1.0 - qab * x / qap
        if abs(d) < tiny:
            d = tiny
        d = 1.0 / d
        h = d
        for m in range(1, 300):
            m2 = 2 * m
            aa = m * (b - m) * x / ((qam + m2) * (a + m2))
            d = 1.0 + aa * d
            if abs(d) < tiny:
                d = tiny
            c = 1.0 + aa / c
            if abs(c) < tiny:
                c = tiny
            d = 1.0 / d
            h *= d * c
            aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
            d = 1.0 + aa * d
            if abs(d) < tiny:
                d = tiny
            c = 1.0 + aa / c
            if abs(c) < tiny:
                c = tiny
            d = 1.0 / d
            delta = d * c
            h *= delta
            if abs(delta - 1.0) < 1e-15:
                break
        return h

    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_0975_quantile(df: int) -> float:
    """Upper 0.975 critical value of Student's t with ``df`` degrees of freedom.

    Uses P(T > t) = 0.5 * I_{df/(df+t^2)}(df/2, 1/2): solve I_x(df/2, 1/2) = 0.05
    for x by bisection, then t = sqrt(df*(1-x)/x). Deterministic."""
    a = df / 2.0
    b = 0.5
    lo, hi = 1e-300, 1.0 - 1e-16
    for _ in range(300):
        mid = 0.5 * (lo + hi)
        if _regularized_incomplete_beta(a, b, mid) > 0.05:
            hi = mid
        else:
            lo = mid
    x = 0.5 * (lo + hi)
    return math.sqrt(df * (1.0 - x) / x)


def generate_value(df: int) -> float:
    """The committed stored value for one df: the quantile rounded to
    STORED_DECIMALS (the exact value materialized in the static table)."""
    return round(student_t_0975_quantile(df), STORED_DECIMALS)


def generate_table(max_df: int = CERTIFIED_MAX_DF) -> tuple:
    """The full committed table as a tuple: index 0 is None (df starts at 1),
    indices 1..max_df hold the critical values."""
    return (None,) + tuple(generate_value(df) for df in range(1, max_df + 1))


def format_literal(max_df: int = CERTIFIED_MAX_DF) -> str:
    """Render the committed table as a reviewable Python tuple literal."""
    vals = [generate_value(df) for df in range(1, max_df + 1)]
    lines = ["# df=0 is an unused placeholder so STUDENT_T_0975[df] indexes by df.",
             "STUDENT_T_0975: tuple = (", "    None,"]
    row: list[str] = []
    for i, v in enumerate(vals, start=1):
        row.append(repr(v) + ",")
        if len(row) == 8:
            lines.append("    " + " ".join(row) + "  # df {}-{}".format(i - 7, i))
            row = []
    if row:
        lines.append("    " + " ".join(row))
    lines.append(")")
    return "\n".join(lines)


def _verify(repo_root: str) -> int:
    from pathlib import Path
    p = Path(repo_root) / "portfolio_automation" / "vs002_evidence" / "result_contract.py"
    ns: dict = {}
    exec(compile(p.read_text(encoding="utf-8"), str(p), "exec"), ns)
    committed = ns["STUDENT_T_0975"]
    ok = True
    for df in range(1, CERTIFIED_MAX_DF + 1):
        if committed[df] != generate_value(df):
            print("MISMATCH df=%d committed=%r generated=%r" % (df, committed[df], generate_value(df)))
            ok = False
    print("VERIFY:", "OK" if ok else "FAILED", "entries=", len(committed) - 1)
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "verify":
        raise SystemExit(_verify("."))
    print(format_literal())
